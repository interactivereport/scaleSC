import os
import logging
import warnings
import cupy as cp 
import numpy as np 
import scanpy as sc
import rapids_singlecell as rsc
from skmisc.loess import loess
from time import time 
from . import util
from .kernels import *

logger = logging.getLogger("scaleSC")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)

def write_to_disk(adata, output_dir, data_name, batch_name=None):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        if batch_name is None:
            out_name = f'{output_dir}/{data_name}.h5ad'
        else:
            out_name = f'{output_dir}/{data_name}.{batch_name}.h5ad'
        adata.write_h5ad(out_name)


class ScaleSC():
    def __init__(self, data_dir, 
                 max_cell_batch=1e5, 
                 preload_on_cpu=True, 
                 preload_on_gpu=True, 
                 save_raw_counts=False,
                 save_norm_counts=False,
                 save_after_each_step=False,
                 output_dir='results',
                 gpus=None):
        self.data_dir = data_dir
        self.max_cell_batch = max_cell_batch
        self.preload_on_cpu = preload_on_cpu
        self.preload_on_gpu = preload_on_gpu
        if preload_on_gpu:
            self.preload_on_cpu = True
        self.save_raw_counts = save_raw_counts
        self.save_norm_counts = save_norm_counts
        self.save_after_each_step = save_after_each_step
        self.output_dir = output_dir
        self.gpus = gpus
        self.data_name = data_dir.split('/')[-1]
        self.norm = False
        self.init()

    @property
    def adata(self):
        assert self.preload_on_cpu and not self.reader.have_looped_once, "adata hasn't been created, call 'batchify()' once to initialize it."
        return self.reader.adata

    @property 
    def adata_X(self):
        return self.reader.get_merged_adata_with_X()
    
    def to_GPU(self):
        self.reader.batch_to_GPU()
    
    def to_CPU(self):
        self.reader.batch_to_CPU()
    
    def clear(self):
        self.reader.clear()

    def init(self):
        assert os.path.exists(self.data_dir), "Data dir is not existed. Please double check and make sure samples have already been split."
        # TODO: walk dir and get size
        self.reader = util.AnnDataBatchReader(data_dir=self.data_dir, 
                                     preload_on_cpu=self.preload_on_cpu, 
                                     preload_on_gpu=self.preload_on_gpu, 
                                     gpus=self.gpus, 
                                     max_cell_batch=self.max_cell_batch) 
        
    def calculate_qc_metrics(self):
        assert self.preload_on_cpu, "Run in mode with preload_on_cpu as False, terminated."
        for d in self.reader.batchify(axis='cell'):
            rsc.pp.calculate_qc_metrics(d)

    def filter_genes(self, min_count=0, max_count=None, qc_var='n_cells_by_counts', qc=False):
        if qc:
            self.calculate_qc_metrics()
        genes_counts = []
        num_cells = 0
        for d in self.reader.batchify(axis='cell'):
            num_cells += d.shape[0]
            genes_counts.append(d.var[qc_var])
        genes_total_counts = np.sum(genes_counts, axis=0)
        if max_count is None:
            max_count = num_cells
        genes_filter = (genes_total_counts >= min_count) & (genes_total_counts <= max_count)
        self.reader.set_genes_filter(genes_filter, update=True if self.preload_on_cpu else False)

    def filter_cells(self, min_count=0, max_count=None, qc_var='n_genes_by_counts', qc=False):
        if qc:
            self.calculate_qc_metrics()
        cells_filter = []
        cell_names = []
        for d in self.reader.batchify(axis='cell'):
            cells_index = util.filter_cells(d, qc_var=qc_var, min_count=min_count, max_count=max_count)
            cells_filter.append(cells_index)
            cell_names += d.obs.index[cells_index].tolist()
        self.reader.set_cells_filter(cells_filter, update=True if self.preload_on_cpu else False)
        self.cell_names = cell_names

    def filter_genes_and_cells(self, min_counts_per_gene=0, min_counts_per_cell=0,
                                max_counts_per_gene=None, max_counts_per_cell=None,
                                qc_var_gene='n_cells_by_counts', qc_var_cell='n_genes_by_counts', 
                                qc=False):
        if qc:
            self.calculate_qc_metrics()
        num_cells = 0
        cells_filter = []
        genes_counts = []
        for d in self.reader.batchify(axis='cell'):
            rsc.pp.calculate_qc_metrics(d)
            cells_index = util.filter_cells(d, qc_var=qc_var_cell, min_count=min_counts_per_cell, max_count=max_counts_per_cell)
            num_cells += d.shape[0]
            cells_filter.append(cells_index)
            genes_counts.append(d.var[qc_var_gene])
        if max_counts_per_gene is None:
            max_counts_per_gene = num_cells
        genes_total_counts = np.sum(genes_counts, axis=0)
        genes_filter = (genes_total_counts >= min_counts_per_gene) & (genes_total_counts <= max_counts_per_gene)
        self.reader.set_cells_filter(cells_filter, update=True if self.preload_on_cpu else False)
        self.reader.set_genes_filter(genes_filter, update=True if self.preload_on_cpu else False)
 

    def highly_variable_genes(self, n_top_genes=4000, method='seurat_v3'):
        valid_methods = ['seurat_v3']
        assert method in valid_methods, NotImplementedError("only seurat_v3 has been implemented yet.")
        N, M = self.reader.shape
        _sum_x = cp.zeros([M], dtype=cp.float64)
        _sum_x_sq = cp.zeros([M], dtype=cp.float64)
        for d in self.reader.batchify(axis='cell'):
            X_batch = d.X
            x_sum, x_sq_sum = util.get_mean_var(X_batch, axis=0)
            _sum_x += x_sum
            _sum_x_sq += x_sq_sum
        mean = _sum_x / N
        var = (_sum_x_sq / N - mean**2) * N / (N-1)  
        estimate_var = cp.zeros(M, dtype=cp.float64)
        x = cp.log10(mean[var > 0])
        y = cp.log10(var[var > 0])
        model = loess(x.get(), y.get(), span=0.3, degree=2) # fix span and degree here
        model.fit()
        estimate_var[var > 0] = model.outputs.fitted_values
        std = cp.sqrt(10**estimate_var)  # TODO: problematic, double check later!!!
        clip_val = std * cp.sqrt(N) + mean
        squared_batch_counts_sum = cp.zeros(clip_val.shape, dtype=cp.float64)
        batch_counts_sum = cp.zeros(clip_val.shape, dtype=cp.float64)
        for d in self.reader.batchify(axis='cell'):
            batch_counts = d.X
            x_sq = cp.zeros_like(squared_batch_counts_sum, dtype=cp.float64)
            x = cp.zeros_like(batch_counts_sum, dtype=cp.float64)
            seurat_v3_elementwise_kernel(batch_counts.data, batch_counts.indices, clip_val, x_sq, x)
            squared_batch_counts_sum += x_sq
            batch_counts_sum += x 
        """
            ** is not correct here
            z = (x-m) / s
            var(z) = E[z^2] - E[z]^2
            E[z^2] = E[x^2 - 2xm + m^2] / s^2
            E[z] = E[x-m] / s
            x is the truncated value x by \sqrt N. m is the mean before trunction, s is the estimated std
            E[z]^2 is supposed to be close to 0.
        """   
        e_z_sq = (1 / ((N - 1) * cp.square(std))) *\
                        (N*cp.square(mean) + squared_batch_counts_sum - 2*batch_counts_sum*mean)
        e_sq_z = (1 / cp.square(std) / (N-1)**2) *\
                        cp.square((squared_batch_counts_sum - N*mean))
        norm_gene_var = e_z_sq         
        ranked_norm_gene_vars = cp.argsort(cp.argsort(-norm_gene_var))
        self.genes_hvg_filter = (ranked_norm_gene_vars < n_top_genes).get() 
        self.adata.var['highly_variable'] = self.genes_hvg_filter
        # reader.set_genes_filter(genes_hvg_filter, update=False) # do not update data, since normalization needs to be performed on all genes after filtering.
        
    def normalize_log1p(self, target_sum=1e4):
        assert self.preload_on_cpu, "count matrix manipulation is disabled when preload_on_cpu is False, call 'normalize_log1p_pca' to perform PCA. "
        for i, d in enumerate(self.reader.batchify(axis='cell')):  # the first loop is used to calculate mean and X.TX
            if self.save_raw_counts:
                write_to_disk(d, output_dir=f'{self.output_dir}/raw_counts', data_name=self.data_name, batch_name=f'batch_{i}')
            rsc.pp.normalize_total(d, target_sum=target_sum)
            rsc.pp.log1p(d)
            if self.save_norm_counts:
                write_to_disk(d, output_dir=f'{self.output_dir}/norm_counts', data_name=self.data_name, batch_name=f'batch_{i}')
        self.norm = True

    def pca(self, n_components=50, hvg_var='highly_variable'):
        if not self.norm:
            warnings.warn("data may haven't been normalized.")
        N, M = self.reader.shape
        genes_hvg_filter = self.adata.var[hvg_var].values
        n_top_genes = sum(self.adata.var['highly_variable'])
        cov = cp.zeros((n_top_genes, n_top_genes), dtype=cp.float64)
        s = cp.zeros((1, n_top_genes), dtype=cp.float64)
        for d in self.reader.batchify(axis='cell'):
            d = d[:, genes_hvg_filter].copy()   # use all genes to normalize instead of hvgs
            X = d.X.toarray() 
            cov += cp.dot(X.T, X)  
            s += X.sum(axis=0, dtype=cp.float64) 
        m = s / N
        cov_norm = cov - cp.dot(m.T, s) - cp.dot(s.T, m) + cp.dot(m.T, m)*N
        eigvecs = cp.linalg.eigh(cov_norm)[1][:, :-n_components-1:-1] # eig values is acsending, eigvecs[:, i] corresponds to the i-th eigvec
        eigvecs = util.svd_flip(eigvecs)
        X_pca = cp.zeros([N, n_components], dtype=cp.float64)
        start_index = 0
        for d in self.reader.batchify(axis='cell'):  # the second loop is used to obtain PCA projection
            d = d[:, genes_hvg_filter].copy()
            X = d.X.toarray()
            X_pca_batch = (X-m) @ eigvecs
            end_index = min(start_index+X_pca_batch.shape[0], N)
            X_pca[start_index:end_index] = X_pca_batch
            start_index = end_index
        X_pca_cpu = X_pca.get()
        # self.reader.set_genes_filter(genes_hvg_filter) # can set or not
        self.adata.obsm['X_pca'] = X_pca_cpu
 
    def normalize_log1p_pca(self, target_sum=1e4, n_components=50, hvg_var='highly_variable'):
        if not self.norm:
            warnings.warn("data may haven't been normalized.")
        N, M = self.reader.shape
        genes_hvg_filter = self.adata.var[hvg_var].values
        n_top_genes = sum(self.adata.var['highly_variable'])
        cov = cp.zeros((n_top_genes, n_top_genes), dtype=cp.float64)
        s = cp.zeros((1, n_top_genes), dtype=cp.float64)
        for i, d in enumerate(self.reader.batchify(axis='cell')):
            if self.save_raw_counts:
                write_to_disk(d, output_dir=f'{self.output_dir}/raw_counts', data_name=self.data_name, batch_name=f'batch_{i}')
            rsc.pp.normalize_total(d, target_sum=target_sum)
            rsc.pp.log1p(d)
            if self.save_norm_counts:
                write_to_disk(d, output_dir=f'{self.output_dir}/norm_counts', data_name=self.data_name, batch_name=f'batch_{i}')
            d = d[:, genes_hvg_filter].copy()   # use all genes to normalize instead of hvgs
            X = d.X.toarray() 
            cov += cp.dot(X.T, X)  
            s += X.sum(axis=0, dtype=cp.float64) 
        m = s / N
        cov_norm = cov - cp.dot(m.T, s) - cp.dot(s.T, m) + cp.dot(m.T, m)*N
        eigvecs = cp.linalg.eigh(cov_norm)[1][:, :-n_components-1:-1] # eig values is acsending, eigvecs[:, i] corresponds to the i-th eigvec
        eigvecs = util.svd_flip(eigvecs)
        X_pca = cp.zeros([N, n_components], dtype=cp.float64)
        start_index = 0
        for d in self.reader.batchify(axis='cell'):  # the second loop is used to obtain PCA projection
            if not self.preload_on_cpu:
                rsc.pp.normalize_total(d, target_sum=target_sum)
                rsc.pp.log1p(d)
            d = d[:, genes_hvg_filter].copy()
            X = d.X.toarray()
            X_pca_batch = (X-m) @ eigvecs
            end_index = min(start_index+X_pca_batch.shape[0], N)
            X_pca[start_index:end_index] = X_pca_batch
            start_index = end_index
        X_pca_cpu = X_pca.get()
        self.reader.set_genes_filter(genes_hvg_filter)
        self.adata.obsm['X_pca'] = X_pca_cpu

    def harmony(self, sample_col_name, n_init=10, max_iter_harmony=20):
        util.harmony(self.adata, key=sample_col_name, init_seeds='2-step', n_init=n_init, max_iter_harmony=max_iter_harmony)
        if self.save_after_each_step:
            self.save(data_name=f'{self.data_name}_after_harmony')

    def neighbors(self, n_neighbors=20, n_pcs=50, use_rep='X_pac_harmony', algorithm='cagra'):
        rsc.pp.neighbors(self.adata, n_neighbors=n_neighbors, n_pcs=n_pcs, use_rep=use_rep, algorithm=algorithm)
        if self.save_after_each_step:
            self.save(data_name=f'{self.data_name}_after_neighbor')
    
    def leiden(self, resolution=0.5, random_state=42):
        rsc.tl.leiden(self.adata, resolution=resolution, random_state=random_state)
        util.correct_leiden(self.adata)
        if self.save_after_each_step:
            self.save(data_name=f'{self.data_name}_after_leiden')

    def umap(self, random_state=42):
        rsc.tl.umap(self.adata, random_state=random_state)
        if self.save_after_each_step:
            self.save(data_name=f'{self.data_name}_after_umap')

    def save(self, data_name=None):
        if data_name is None:
            data_name = self.data_name
        write_to_disk(adata=self.adata, output_dir=self.output_dir, data_name=data_name)

    def savex(self, name, data_name=None):
        if data_name is None:
            data_name = self.data_name
        for i, d in enumerate(self.reader.batchify(axis='cell')):
            write_to_disk(d, output_dir=f'{self.output_dir}/{name}', batch_name=f'batch_{i}', data_name=data_name)

        
# if __name__ == 'scalesc.pp':
#     scalesc = ScaleSC(data_dir='/edgehpc/dept/compbio/projects/scaleSC/haotian/batch/data_dir/70k_human_lung')
#     scalesc.calculate_qc_metrics()
#     scalesc.filter_genes(min_count=3)
#     scalesc.filter_cells(min_count=200, max_count=6000)
#     scalesc.highly_variable_genes(n_top_genes=4000)
#     scalesc.normalize_log1p()
#     scalesc.pca(n_components=50)
#     scalesc.to_CPU()
#     print(scalesc.adata_X)
    

