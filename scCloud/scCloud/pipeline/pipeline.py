import os
import numpy as np
import anndata
from scipy.sparse import csr_matrix, hstack
from .. import tools 

def run_pipeline(input_file, output_name, **kwargs):
	is_raw = not kwargs['processed']

	# load input data
	if not kwargs['cite_seq']:
		adata = tools.read_input(input_file, genome = kwargs['genome'], mode = 'a' if kwargs['subcluster'] else 'r+')
	else:
		data_dict = tools.read_input(input_file, genome = kwargs['genome'], return_a_dict = True)
		assert len(data_dict) == 2
		adata = cdata = None
		for genome, data in data_dict.items():
			if genome.startswith('CITE_Seq'):
				cdata = data
			else:
				adata = data
		assert adata is not None and cdata is not None
	print("Inputs are loaded.")

	# preprocessing
	if is_raw:
		# make gene names unique
		tools.update_var_names(adata)
		# filter out low quality cells/genes
		tools.filter_data(adata, mito_prefix = kwargs['mito_prefix'], filt_xlsx = kwargs['filt_xlsx'], min_genes = kwargs['min_genes'], max_genes = kwargs['max_genes'], percent_mito = kwargs['percent_mito'], percent_cells = kwargs['percent_cells'])
		# normailize counts and then transform to log space
		tools.log_norm(adata, kwargs['norm_count'])
		# estimate bias factors
		if kwargs['batch_correction']:
			tools.set_group_attribute(adata, kwargs['group_attribute'])
			tools.estimate_adjustment_matrices(adata)
	elif kwargs['subcluster']:
		adata = tools.get_anndata_for_subclustering(adata, kwargs['subset_selections'])
		is_raw = True # get submat and then set is_raw to True


	if kwargs['output_seurat_compatible']:
		assert is_raw and kwargs['select_variable_genes'] and kwargs['submat_to_dense']	


	# dimension reduction --- select variable genes or not
	pca_key = kwargs['pca_key']
	if is_raw:	
		if kwargs['select_variable_genes']:
			filter_result = tools.filter_genes_dispersion(adata, kwargs['batch_correction'])
			adata_c = tools.collect_variable_gene_matrix(adata, filter_result.gene_subset)
			if kwargs['submat_to_dense']:
				adata_c.X = adata_c.X.toarray()
			if kwargs['batch_correction']:
				tools.correct_batch_effects(adata_c)
		
			# dimension reduction
			if pca_key == 'X_pca':
				tools.run_pca(adata_c, nPC = kwargs['nPC'], random_state = kwargs['random_state'])
			else:
				tools.run_rpca(adata_c, nPC = kwargs['nPC'], random_state = kwargs['random_state'])
			adata.obsm[pca_key] = adata_c.obsm[pca_key]
		else:
			assert pca_key == 'X_rpca'
			if kwargs['batch_correction']:
				tools.correct_batch_effects(adata)
			tools.run_rpca(adata, nPC = kwargs['nPC'], random_state = kwargs['random_state'])
	else:
		assert pca_key in adata.obsm.keys()

	# diffusion map
	if is_raw:
		tools.run_diffmap(adata, pca_key, n_jobs = kwargs['n_jobs'], n_components = kwargs['nDC'], alpha = kwargs['diffmap_alpha'], K = kwargs['diffmap_K'], random_state = kwargs['random_state'], full_speed = kwargs['diffmap_full_speed'])
	else:
		assert 'X_diffmap' in adata.obsm.keys()

	# clustering
	if kwargs['run_approx_louvain']:
		tools.run_approximated_louvain(adata, 'X_diffmap', n_jobs = kwargs['n_jobs'], resolution = kwargs['approx_louvain_resolution'], random_state = kwargs['random_state'], n_clusters = kwargs['approx_louvain_nclusters'], n_init = kwargs['approx_louvain_ninit'])
	if kwargs['run_kmeans']:
		tools.run_kmeans(adata, 'X_diffmap', kwargs['kmeans_n_clusters'], n_jobs = kwargs['n_jobs'], random_state = kwargs['random_state'])
	if kwargs['run_louvain']:
		tools.run_louvain(adata, affinity = kwargs['louvain_affinity'], resolution = kwargs['louvain_resolution'], random_state = kwargs['random_state'])
	if kwargs['run_hdbscan']:
		tools.run_hdbscan(adata, 'X_diffmap', n_jobs = kwargs['n_jobs'], min_cluster_size = kwargs['hdbscan_min_cluster_size'], min_samples = kwargs['hdbscan_min_samples'])

	# visualization
	if kwargs['run_tsne']:
		tools.run_tsne(adata, pca_key, n_jobs = kwargs['n_jobs'], perplexity = kwargs['tsne_perplexity'], random_state = kwargs['random_state'])
	if kwargs['run_fitsne']:
		tools.run_fitsne(adata, pca_key, n_jobs = kwargs['n_jobs'], perplexity = kwargs['tsne_perplexity'], random_state = kwargs['random_state'])
	if kwargs['run_umap']:
		if kwargs['run_umap_on_diffmap']:
			tools.run_umap(adata, 'X_diffmap', n_neighbors = kwargs['umap_K'], min_dist = kwargs['umap_min_dist'], spread = kwargs['umap_spread'], random_state = kwargs['random_state'])
			adata.obsm['X_umap_diffmap'] = adata.obsm['X_umap']
		tools.run_umap(adata, pca_key, n_neighbors = kwargs['umap_K'], min_dist = kwargs['umap_min_dist'], spread = kwargs['umap_spread'], random_state = kwargs['random_state'])
	if kwargs['run_fle']:
		tools.run_force_directed_layout(adata, output_name, affinity = kwargs['fle_affinity'], n_jobs = kwargs['n_jobs'], K = kwargs['fle_K'], n_steps = kwargs['fle_n_steps'])	
	
	# calculate diffusion-based pseudotime from roots
	if kwargs['pseudotime'] is not None:
		assert 'X_diffmap' in adata.obsm.keys()
		tools.run_pseudotime_calculation(adata, kwargs['pseudotime'])

	# merge cite-seq data and run t-SNE
	if kwargs['cite_seq']:
		adt_matrix = np.zeros((adata.shape[0], cdata.shape[1]), dtype = 'float32')
		idx = adata.obs_names.isin(cdata.obs_names)
		adt_matrix[idx, :] = cdata[adata.obs_names[idx],].X.toarray()

		var_names = np.concatenate([adata.var_names, ['AD-' + x for x in cdata.var_names]])

		new_data = anndata.AnnData(X = hstack([adata.X, csr_matrix(adt_matrix)], format = 'csr'), 
			obs = adata.obs,
			obsm = adata.obsm,
			uns = adata.uns, 
			var = {'var_names' : var_names,
				   'gene_ids' : var_names,
				   'n_cells' : np.concatenate([adata.var['n_cells'].values, [0] * cdata.shape[1]]),
				   'percent_cells' : np.concatenate([adata.var['percent_cells'].values, [0.0] * cdata.shape[1]]), 
				   'robust' : np.concatenate([adata.var['robust'].values, [False] * cdata.shape[1]])
				  })
		if 'selected' in adata.var:
			new_data.var['selected'] = np.concatenate([adata.var['selected'].values, [False] * cdata.shape[1]])
		new_data.obsm['CITE-Seq'] = adt_matrix
		adata = new_data
		print("ADT count matrix is attached.")

		tools.run_tsne(adata, 'CITE-Seq', n_jobs = kwargs['n_jobs'], perplexity = kwargs['tsne_perplexity'], random_state = kwargs['random_state'], out_basis = 'citeseq_tsne')
		print("Antibody embedding is done.")

	# write out results
	adata.write(output_name + ".h5ad")

	if kwargs['output_seurat_compatible']:
		adata_c.obs = adata.obs
		adata_c.obsm = adata.obsm
		adata_c.uns = adata.uns
		adata_c.raw = adata
		adata_c.write(output_name + ".seurat.h5ad")

	if kwargs['output_loom']:
		adata.write_loom(output_name + ".loom")

	print("Results are written.")