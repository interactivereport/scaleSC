# haotian
# harmonypy - A data alignment algorithm.
# Copyright (C) 2018  Ilya Korsunsky
#               2019  Kamil Slowikowski <kslowikowski@gmail.com>
#               2022  Severin Dicks <severin.dicks@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations
# from memory_profiler import profile
# from test import get_usage
import logging
import tracemalloc
import cupy as cp
import numpy as np
import pandas as pd
from cuml import KMeans as KMeans_gpu
from sklearn.cluster import KMeans as KMeans_cpu
from sklearn.cluster import MiniBatchKMeans
from sklearn.cluster import kmeans_plusplus
from sklearn.utils import check_random_state
from cupyx.scipy.sparse import csr_matrix as csr_matrix_cuda, csc_matrix as csc_matrix_cuda
from scipy.sparse import csr_matrix, csc_matrix, coo_matrix
from scipy.sparse import issparse, vstack

# create logger
logger = logging.getLogger("scaleSC")
# logger.setLevel(logging.DEBUG)
# ch = logging.StreamHandler()
# ch.setLevel(logging.DEBUG)
# formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# ch.setFormatter(formatter)
# logger.addHandler(ch)

# from IPython.core.debugger import set_trace

def get_usage(s):
    pass

def to_csr_cuda(x, dtype):
    """ Move to GPU as a csr_matrix. """
    # if issparse(x):
    #     return csr_matrix_cuda(x, dtype=dtype)
    return csr_matrix_cuda(csr_matrix(x, dtype=dtype))

def to_csc_cuda(x, dtype):
    """ Move to GPU as a csc_matrix, speed up column slice. """
    # if issparse(x):
    #     return csc_matrix_cuda(x, dtype=dtype)
    return csc_matrix_cuda(csc_matrix(x, dtype=dtype))

def get_dummies(x):
    """ Return a sparse dummy matrix. """
    x = x.to_numpy().flatten()
    data = np.zeros(x.shape[0], dtype=np.float32)
    i = np.zeros(x.shape[0], dtype=int)
    j = np.zeros(x.shape[0], dtype=int)
    categories = pd.Categorical(x).categories.tolist()
    categories = {cat: p for p, cat in enumerate(categories)}
    for p in range(len(x)):
        q = categories[x[p]]
        data[p] = 1
        i[p] = q
        j[p] = p
    return coo_matrix((data, (i,j)), shape=(len(categories), len(x)))


# @profile
def run_harmony(
    data_mat: np.ndarray,
    meta_data: pd.DataFrame,
    vars_use,
    *,
    init_seeds=None,
    theta=None,
    lamb=None,
    sigma=0.1,
    nclust=None,
    tau=0,
    block_size=0.05,
    max_iter_harmony=10,
    max_iter_kmeans=20,
    epsilon_cluster=1e-5,
    epsilon_harmony=1e-4,
    plot_convergence=False,
    verbose=True,
    reference_values=None,
    cluster_prior=None,
    n_init=1,
    random_state=0,
    dtype=cp.float32,
):
    """Run Harmony."""
    # init_seeds = None
    # theta = None
    # lamb = None
    # sigma = 0.1
    # nclust = None
    # tau = 0
    # block_size = 0.05
    # epsilon_cluster = 1e-5
    # epsilon_harmony = 1e-4
    # plot_convergence = False
    # verbose = True
    # reference_values = None
    # cluster_prior = None
    # random_state = 0
    get_usage('start')
    N = meta_data.shape[0]
    if data_mat.shape[1] != N:
        # force to shape (n_pc, n_data)
        data_mat = data_mat.T

    assert (data_mat.shape[1] == N), "data_mat and meta_data do not have the same number of cells"

    if nclust is None:
        nclust = np.min([np.round(N / 30.0), 100]).astype(int)

    if isinstance(sigma, float) and nclust > 1:
        sigma = np.repeat(sigma, nclust)

    if isinstance(vars_use, str):
        vars_use = [vars_use]

    # phi2 = pd.get_dummies(meta_data[vars_use]).to_numpy().T
    phi = get_dummies(meta_data[vars_use])
    phi_n = meta_data[vars_use].describe().loc["unique"].to_numpy().astype(int)
    # print('phi_n', phi_n)
    if theta is None:
        theta = np.repeat([1] * len(phi_n), phi_n)
    elif isinstance(theta, float) or isinstance(theta, int):
        theta = np.repeat([theta] * len(phi_n), phi_n)
    elif len(theta) == len(phi_n):
        theta = np.repeat([theta], phi_n)

    assert len(theta) == np.sum(phi_n), "each batch variable must have a theta"

    if lamb is None:
        lamb = np.repeat([1] * len(phi_n), phi_n)
    elif isinstance(lamb, float) or isinstance(lamb, int):
        lamb = np.repeat([lamb] * len(phi_n), phi_n)
    elif len(lamb) == len(phi_n):
        lamb = np.repeat([lamb], phi_n)

    assert len(lamb) == np.sum(phi_n), "each batch variable must have a lambda"

    # Number of items in each category.

    N_b = np.asarray(phi.sum(axis=1)).reshape(-1)
    # N_b2 = phi2.sum(axis=1)
    # Proportion of items in each category.
    Pr_b = N_b / N
    if tau > 0:
        theta = theta * (1 - np.exp(-((N_b / (nclust * tau)) ** 2)))

    lamb_mat = np.diag(np.insert(lamb, 0, 0))

    # phi_moe2 = np.vstack((np.repeat(1, N), phi2))
    phi_moe = vstack((np.ones((1, N)), phi))
    get_usage('before random')
    cp.random.seed(random_state)
    np.random.seed(random_state)
    get_usage('data_prepare')
    ho = Harmony(
        data_mat,
        init_seeds,
        n_init,
        phi,
        phi_moe,
        Pr_b,
        sigma,
        theta,
        max_iter_harmony,
        max_iter_kmeans,
        epsilon_cluster,
        epsilon_harmony,
        nclust,
        block_size,
        lamb_mat,
        verbose,
        random_state,
        dtype=dtype,
    )

    # breakpoint()
    return ho


class Harmony:
    # @profile
    def __init__(
        self,
        Z,
        init_seeds,
        n_init,
        Phi,
        Phi_moe,
        Pr_b,
        sigma,
        theta,
        max_iter_harmony,
        max_iter_kmeans,
        epsilon_kmeans,
        epsilon_harmony,
        K,
        block_size,
        lamb,
        verbose,
        random_state,
        dtype,
    ):
    
        self.Z_corr = cp.array(Z, dtype=dtype)
        self.Z_orig = cp.array(Z, dtype=dtype)
    
        self.Z_cos = self.Z_orig / self.Z_orig.max(axis=0)
        self.Z_cos = self.Z_cos / cp.linalg.norm(self.Z_cos, ord=2, axis=0)

        self.init_seeds = init_seeds
        self.n_init = n_init
        # self.Phi = cp.array(Phi, dtype=dtype)
        self.Phi = to_csc_cuda(Phi, dtype=dtype)
        # self.Phi_moe = cp.array(Phi_moe, dtype=dtype)
        self.Phi_moe = to_csr_cuda(Phi_moe, dtype=dtype)
        self.N = self.Z_corr.shape[1]
        self.Pr_b = cp.array(Pr_b, dtype=dtype)
        self.B = self.Phi.shape[0]  # number of batch variables
        self.d = self.Z_corr.shape[0]
        self.window_size = 3
        self.epsilon_kmeans = epsilon_kmeans
        self.epsilon_harmony = epsilon_harmony

        self.lamb = cp.array(lamb, dtype=dtype)
        self.sigma = cp.array(sigma, dtype=dtype)
        self.sigma_prior = cp.array(sigma, dtype=dtype)
        self.block_size = block_size
        self.K = K  # number of clusters
        self.max_iter_harmony = max_iter_harmony
        self.max_iter_kmeans = max_iter_kmeans
        self.verbose = verbose
        self.theta = cp.array(theta, dtype=dtype)
        # self.random_state = random_state
        self.random_state = check_random_state(random_state) # get a RNG instance

        self.objective_harmony = []
        self.objective_kmeans = []
        self.objective_kmeans_dist = []
        self.objective_kmeans_entropy = []
        self.objective_kmeans_cross = []
        self.kmeans_rounds = []
        self.dtype = dtype
        get_usage('copy to gpu')
        self.allocate_buffers()
        get_usage('allocate buffer')
        self.init_cluster()
        get_usage('init cluster')
        self.harmonize(self.max_iter_harmony, self.verbose)
        get_usage('iter')

    def result(self):
        return self.Z_corr.T.get()

    def allocate_buffers(self):
        # self._scale_dist = cp.zeros((self.K, self.N), dtype=self.dtype)
        self.dist_mat = cp.zeros((self.K, self.N), dtype=self.dtype)
        self.O = cp.zeros((self.K, self.B), dtype=self.dtype)
        self.E = cp.zeros((self.K, self.B), dtype=self.dtype)
        self.W = cp.zeros((self.B + 1, self.d), dtype=self.dtype)
        # self.Phi_Rk = cp.zeros((self.B + 1, self.N), dtype=self.dtype) 
        self.Phi_Rk = to_csr_cuda(np.zeros((self.B + 1, self.N), dtype=self.dtype), dtype=self.dtype) # to csr


    def kmeans_multirestart(self):
        Z_cos_cpu = self.Z_cos.T.get()
        best_centers = None
        best_score = None
        for i in range(self.n_init):
            center, indices = kmeans_plusplus(Z_cos_cpu, n_clusters=self.K, random_state=self.random_state)
            kmeans_obj = KMeans_gpu(n_clusters=self.K, init=center, n_init=1, max_iter=25).fit(Z_cos_cpu)
            score = kmeans_obj.inertia_
            if best_score is None:
                best_score = score
            else:
                if score < best_score:
                    best_score = score
                    best_centers = kmeans_obj.cluster_centers_
        return best_centers
    
    # @profile
    def init_cluster(self):
        best_cluster_centers = None
        # if none then perform two-step correction as default
        if self.init_seeds is None or self.init_seeds == '2-step':
            best_cluster_centers = self.kmeans_multirestart()
        elif self.init_seeds == '1-step':
            Z_cos_cpu = self.Z_cos.T.get()
            kmeans_obj = KMeans_cpu(n_clusters=self.K, init='k-means++', n_init=self.n_init, max_iter=25, random_state=self.random_state).fit(Z_cos_cpu)
            best_cluster_centers = kmeans_obj.cluster_centers_
        elif self.init_seeds == 'rapids':
            kmeans_obj = KMeans_gpu(n_clusters=self.K, random_state=0, init="k-means||", max_iter=25, n_init=self.n_init).fit(self.Z_cos.T)
            best_cluster_centers = kmeans_obj.cluster_centers_

        # self.Y = kmeans_obj.cluster_centers_.T
        self.Y = cp.array(best_cluster_centers.T)
        # (1) Normalize
        self.Y = self.Y / cp.linalg.norm(self.Y, ord=2, axis=0)
        # (2) Assign cluster probabilities
        self.dist_mat = 2 * (1 - cp.dot(self.Y.T, self.Z_cos))
        self.R = -self.dist_mat
        self.R = self.R / self.sigma[:, None]
        self.R -= cp.max(self.R, axis=0)
        self.R = cp.exp(self.R)
        self.R = self.R / cp.sum(self.R, axis=0)
        # (3) Batch diversity statistics
        self.E = cp.outer(cp.sum(self.R, axis=1), self.Pr_b)
        self.O = self.R @ self.Phi.T
        # self.O = cp.inner(self.R, self.Phi)
        self.compute_objective()
        # Save results
        self.objective_harmony.append(self.objective_kmeans[-1])

    def compute_objective(self):
        get_usage('---s')
        kmeans_error = cp.sum(cp.multiply(self.R, self.dist_mat))
        # Entropy
        _entropy = cp.sum(safe_entropy(self.R) * self.sigma[:, cp.newaxis])
        # Cross Entropy
        x = self.R * self.sigma[:, cp.newaxis]
        y = cp.tile(self.theta[:, cp.newaxis], self.K).T
        z = cp.log((self.O + 1) / (self.E + 1))
        # w = cp.dot(y * z, self.Phi)
        w =  (y*z) @ self.Phi
        _cross_entropy = cp.sum(x * w)
        get_usage('---!')
        # Save results
        self.objective_kmeans.append(kmeans_error + _entropy + _cross_entropy)
        self.objective_kmeans_dist.append(kmeans_error)
        self.objective_kmeans_entropy.append(_entropy)
        self.objective_kmeans_cross.append(_cross_entropy)

    # @profile
    def harmonize(self, iter_harmony=10, verbose=True):
        converged = False
        for i in range(1, iter_harmony + 1):
            if verbose:
                logger.debug(f"Harmony: Iteration {i} of {iter_harmony}")
            # STEP 1: Clustering
            self.cluster()
            # STEP 2: Regress out covariates
            # self.moe_correct_ridge()
            self.Z_cos, self.Z_corr, self.W, self.Phi_Rk = moe_correct_ridge(
                self.Z_orig,
                self.Z_cos,
                self.Z_corr,
                self.R,
                self.W,
                self.K,
                self.Phi_Rk,
                self.Phi_moe,
                self.lamb,
            )
            # STEP 3: Check for convergence
            converged = self.check_convergence(1)
            if converged:
                if verbose:
                    logger.info(
                        "Harmony: Converged after {} iteration{}".format(i, "s" if i > 1 else "")
                    )
                break
        if verbose and not converged:
            logger.info("Harmony: Stopped before convergence")
        return 0

    def cluster(self):
        get_usage('--cluster')
        # Z_cos has changed
        # R is assumed to not have changed
        # Update Y to match new integrated data
        self.dist_mat = 2 * (1 - cp.dot(self.Y.T, self.Z_cos))
        for i in range(self.max_iter_kmeans):
            # print("kmeans {}".format(i))
            # STEP 1: Update Y
            self.Y = cp.dot(self.Z_cos, self.R.T)
            self.Y = self.Y / cp.linalg.norm(self.Y, ord=2, axis=0)
            # STEP 2: Update dist_mat
            self.dist_mat = 2 * (1 - cp.dot(self.Y.T, self.Z_cos))
            # STEP 3: Update R
            self.update_R()
            # STEP 4: Check for convergence
            self.compute_objective()
            if i > self.window_size:
                converged = self.check_convergence(0)
                if converged:
                    break
        self.kmeans_rounds.append(i)
        self.objective_harmony.append(self.objective_kmeans[-1])
        get_usage('--end cluster')
        return 0

    def update_R(self):
        get_usage('update R')
        # self._scale_dist = -self.dist_mat
        # self._scale_dist = self._scale_dist / self.sigma[:, None]
        # self._scale_dist -= cp.max(self._scale_dist, axis=0)
        # self._scale_dist = cp.exp(self._scale_dist)
        _scale_dist = -self.dist_mat
        _scale_dist = _scale_dist / self.sigma[:, None]
        _scale_dist -= cp.max(_scale_dist, axis=0)
        _scale_dist = cp.exp(_scale_dist)
        
        # Update cells in blocks
        # update_order = cp.arange(self.N)
        # cp.random.shuffle(update_order)
        update_order = np.arange(self.N)
        np.random.shuffle(update_order)
        update_order = cp.array(update_order)
        # print(update_order)
        n_blocks = cp.ceil(1 / self.block_size).astype(int)
        blocks = cp.array_split(update_order, int(n_blocks))
        for b in blocks:
            # print('b', b)
            # print('R', self.R.shape)
            # print('Phi', self.Phi.shape)
            # STEP 1: Remove cells
            self.E -= cp.outer(cp.sum(self.R[:, b], axis=1), self.Pr_b)
            # self.O -= cp.dot(self.R[:, b], self.Phi[:, b].T)
            self.O -= self.R[:, b] @ self.Phi[:, b].T
            # STEP 2: Recompute R for removed cells
            # self.R[:, b] = self._scale_dist[:, b]
            self.R[:, b] = _scale_dist[:, b]
            self.R[:, b] = cp.multiply(
                self.R[:, b],
                cp.dot(
                    cp.power((self.E + 1) / (self.O + 1), self.theta), self.Phi[:, b].toarray()
                ),
            )
            self.R[:, b] = self.R[:, b] / cp.linalg.norm(self.R[:, b], ord=1, axis=0)
            # STEP 3: Put cells back
            self.E += cp.outer(cp.sum(self.R[:, b], axis=1), self.Pr_b)
            # self.O += cp.dot(self.R[:, b], self.Phi[:, b].T)
            self.O += self.R[:, b] @ self.Phi[:, b].T
        get_usage('end update R')
        return 0

    def check_convergence(self, i_type):
        obj_old = 0.0
        obj_new = 0.0
        # Clustering, compute new window mean
        if i_type == 0:
            okl = len(self.objective_kmeans)
            for i in range(self.window_size):
                obj_old += self.objective_kmeans[okl - 2 - i]
                obj_new += self.objective_kmeans[okl - 1 - i]
            if abs(obj_old - obj_new) / abs(obj_old) < self.epsilon_kmeans:
                return True
            return False
        # Harmony
        if i_type == 1:
            obj_old = self.objective_harmony[-2]
            obj_new = self.objective_harmony[-1]
            if (obj_old - obj_new) / abs(obj_old) < self.epsilon_harmony:
                return True
            return False
        return True


def safe_entropy(x: cp.array):
    y = cp.multiply(x, cp.log(x))
    y[~cp.isfinite(y)] = 0.0
    return y


# def moe_correct_ridge(Z_orig, Z_cos, Z_corr, R, W, K, Phi_Rk, Phi_moe, lamb):
#     Z_corr = Z_orig.copy()
#     for i in range(K):
#         Phi_Rk = cp.multiply(Phi_moe, R[i, :])
#         x = cp.dot(Phi_Rk, Phi_moe.T) + lamb
#         W = cp.dot(cp.dot(cp.linalg.inv(x), Phi_Rk), Z_orig.T)
#         W[0, :] = 0  # do not remove the intercept
#         Z_corr -= cp.dot(W.T, Phi_Rk)
#     Z_cos = Z_corr / cp.linalg.norm(Z_corr, ord=2, axis=0)
#     return Z_cos, Z_corr, W, Phi_Rk

def moe_correct_ridge(Z_orig, Z_cos, Z_corr, R, W, K, Phi_Rk, Phi_moe, lamb):
    get_usage('moe start')
    Z_corr = Z_orig.copy()
    for i in range(K):
        # Phi_Rk = cp.multiply(Phi_moe, R[i, :])
        Phi_Rk = Phi_moe.multiply(R[i, :])
        x = (Phi_Rk @ Phi_moe.T) + lamb
        # x = cp.dot(Phi_Rk, Phi_moe.T) + lamb
        # W = (cp.linalg.inv(x) @ Phi_Rk).dot(Z_orig.T)  
        # W = (csr_matrix_cuda(cp.linalg.inv(x)) @ Phi_Rk).dot(Z_orig.T)  
        # W = (cp.linalg.inv(x) @ Phi_Rk) @ Z_orig.T

        W = cp.linalg.inv(x) @ (Phi_Rk @ Z_orig.T)
        # W = cp.dot(cp.dot(cp.linalg.inv(x), Phi_Rk), Z_orig.T)
        W[0, :] = 0  # do not remove the intercept

        Z_corr -= W.T @ Phi_Rk
        # Z_corr -= cp.dot(W.T, Phi_Rk)
    Z_cos = Z_corr / cp.linalg.norm(Z_corr, ord=2, axis=0)
    get_usage('moe end ')
    return Z_cos, Z_corr, W, Phi_Rk