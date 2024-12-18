import argparse
import shutil
import os
import numpy as np
import torch
import torch.nn.functional as F
from tqdm.auto import tqdm, trange
from scipy.sparse import csr_matrix, vstack, SparseEfficiencyWarning, diags, \
	hstack
from concurrent.futures import ProcessPoolExecutor, as_completed
import h5py, math
import pandas as pd

# try:
# 	get_ipython()
# 	print ("jupyter notebook mode")
# 	from tqdm.notebook import tqdm, trange
# except Exception as e:
# 	print (e)
# 	print ("terminal mode")
# 	pass

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device_ids = [0, 1]

def get_config(config_path = "./config.jSON"):
	import json
	c = open(config_path,"r")
	return json.load(c)


def parse_args():
	parser = argparse.ArgumentParser(description="Higashi Processing")
	parser.add_argument('-c', '--config', type=str, default="./config.JSON")
	return parser.parse_args()


def get_free_gpu():
    # Get the list of allocated GPUs from CUDA_VISIBLE_DEVICES
    visible_devices = os.environ.get('CUDA_VISIBLE_DEVICES', None)
    
    if visible_devices is None:
        print("No GPUs allocated. Using CPU.")
        return None  # No GPUs are allocated; fallback to CPU

    # Map visible devices to physical device IDs
    visible_devices = [int(x) for x in visible_devices.split(',')]

    # Query free memory for allocated GPUs
    free_memory = []
    for gpu_id in visible_devices:
        try:
            # Calculate available memory for each GPU
            memory_free = torch.cuda.get_device_properties(gpu_id).total_memory - torch.cuda.memory_reserved(gpu_id)
            free_memory.append(memory_free)
        except Exception as e:
            print(f"Error querying GPU {gpu_id}: {e}")
            free_memory.append(0)

    if len(free_memory) > 0:
        # Select GPU with the maximum available memory
        max_mem = np.max(free_memory)
        ids = np.where(np.array(free_memory) == max_mem)[0]
        chosen_id = int(np.random.choice(ids, 1)[0])
        global_gpu_id = visible_devices[chosen_id]  # Map back to global GPU ID
        print(f"Setting to GPU:{global_gpu_id}")
        torch.cuda.set_device(global_gpu_id)
    else:
        print("No free GPU found. Using CPU.")
        return None


def create_dir(config):
	temp_dir = config['temp_dir']
	if not os.path.exists(temp_dir):
		os.mkdir(temp_dir)
	
	raw_dir = os.path.join(temp_dir, "raw")
	if not os.path.exists(raw_dir):
		os.mkdir(raw_dir)
	
	rw_dir = os.path.join(temp_dir, "rw")
	if not os.path.exists(rw_dir):
		os.mkdir(rw_dir)
	
	embed_dir = os.path.join(temp_dir, "embed")
	if not os.path.exists(embed_dir):
		os.mkdir(embed_dir)


# Generate a indexing table of start and end id of each chromosome
def generate_chrom_start_end(config):
	# fetch info from config
	genome_reference_path = config['genome_reference_path']
	chrom_list = config['chrom_list']
	res = config['resolution']
	temp_dir = config['temp_dir']
	
	print("generating start/end dict for chromosome")
	chrom_size = pd.read_table(genome_reference_path, sep="\t", header=None)
	chrom_size.columns = ['chrom', 'size']
	# build a list that stores the start and end of each chromosome (unit of the number of bins)
	chrom_start_end = np.zeros((len(chrom_list), 2), dtype='int')
	for i, chrom in enumerate(chrom_list):
		size = chrom_size[chrom_size['chrom'] == chrom]
		size = size['size'][size.index[0]]
		n_bin = int(math.ceil(size / res))
		chrom_start_end[i, 1] = chrom_start_end[i, 0] + n_bin
		if i + 1 < len(chrom_list):
			chrom_start_end[i + 1, 0] = chrom_start_end[i, 1]
	
	# print("chrom_start_end", chrom_start_end)
	np.save(os.path.join(temp_dir, "chrom_start_end.npy"), chrom_start_end)


def data2mtx(config, file, chrom_start_end, verbose, cell_id, blacklist=""):
	if type(file) is str:
		if "header_included" in config:
			if config['header_included']:
				tab = pd.read_table(file, sep="\t")
			else:
				tab = pd.read_table(file, sep="\t", header=None)
				tab.columns = config['contact_header'][:len(tab.columns)]
		else:
			tab = pd.read_table(file, sep="\t", header=None)
			tab.columns = config['contact_header']
		if 'count' not in tab.columns:
			tab['count'] = 1
	else:
		tab = file
	
	data = tab
	# fetch info from config
	res = config['resolution']
	chrom_list = config['chrom_list']
	
	data = data[(data['chrom1'] == data['chrom2']) & ((np.abs(data['pos2'] - data['pos1']) >= 2500) | (np.abs(data['pos2'] - data['pos1']) == 0))]

	if blacklist != "" and len(data) > 0:
		data = remove_blacklist(blacklist, data)

	pos1 = np.array(data['pos1'])
	pos2 = np.array(data['pos2'])
	bin1 = np.floor(pos1 / res).astype('int')
	bin2 = np.floor(pos2 / res).astype('int')
	
	chrom1, chrom2 = np.array(data['chrom1'].values), np.array(data['chrom2'].values)
	count = np.array(data['count'].values)
	
	del data
	
	m1_list = []
	for i, chrom in enumerate(chrom_list):
		mask = (chrom1 == chrom)  # & (bin1 != bin2)
		size = chrom_start_end[i, 1] - chrom_start_end[i, 0]
		temp_weight2 = count[mask]
		m1 = csr_matrix((temp_weight2, (bin1[mask], bin2[mask])), shape=(size, size), dtype='float32')
		m1 = m1 + m1.T
		m1_list.append(m1)
		count = count[~mask]
		bin1 = bin1[~mask]
		bin2 = bin2[~mask]
		chrom1 = chrom1[~mask]
	
	return m1_list, cell_id


# Extra the data.txt table
# Memory consumption re-optimize
def extract_table(config):
	# fetch info from config
	data_dir = config['data_dir']
	temp_dir = config['temp_dir']
	chrom_list = config['chrom_list']
	if "blacklist" in config:
		blacklist = config["blacklist"]
	else:
		blacklist = ""
	if 'input_format' in config:
		input_format = config['input_format']
	else:
		input_format = 'higashi_v1'
	
	chrom_start_end = np.load(os.path.join(temp_dir, "chrom_start_end.npy"))
	import multiprocessing
	cpu_num = multiprocessing.cpu_count()
	if input_format == 'higashi_v1':
		print("extracting from data.txt")
		if "structured" in config:
			if config["structured"]:
				chunksize = int(5e6)
				cell_tab = []

				p_list = []
				pool = ProcessPoolExecutor(max_workers=cpu_num)
				print("First calculating how many lines are there")
				line_count = sum(1 for i in open(os.path.join(data_dir, "data.txt"), 'rb'))
				print("There are %d lines" % line_count)
				bar = trange(line_count, desc=' - Processing ', leave=False, )
				cell_num = 0
				with open(os.path.join(data_dir, "data.txt"), 'r') as csv_file:
					chunk_count = 0
					reader = pd.read_csv(csv_file, chunksize=chunksize, sep="\t")
					for chunk in reader:
						if len(chunk['cell_id'].unique()) == 1:
							# Only one cell, keep appending
							cell_tab.append(chunk)
						else:
							# More than one cell, append all but the last part
							last_cell = np.array(chunk.tail(1)['cell_id'])[0]
							tails = chunk.iloc[np.array(chunk['cell_id']) != last_cell, :]
							head = chunk.iloc[np.array(chunk['cell_id']) == last_cell, :]
							cell_tab.append(tails)
							cell_tab = pd.concat(cell_tab, axis=0).reset_index()
							for cell_id in np.unique(cell_tab['cell_id']):
								p_list.append(
									pool.submit(data2mtx, config, cell_tab[cell_tab['cell_id'] == cell_id].reset_index(),
									            chrom_start_end, False, cell_id, blacklist))
								cell_num = max(cell_num, cell_id + 1)

							cell_tab = [head]
							bar.update(n=chunksize)
							bar.refresh()


				if len(cell_tab) != 0:
					cell_tab = pd.concat(cell_tab, axis=0).reset_index()
					for cell_id in np.unique(cell_tab['cell_id']):
						p_list.append(
							pool.submit(data2mtx, config, cell_tab[cell_tab['cell_id'] == cell_id].reset_index(),
							            chrom_start_end, False, cell_id, blacklist))
						cell_num = max(cell_num, cell_id + 1)
				cell_num = int(cell_num)
				mtx_all_list = [[0] * cell_num for i in range(len(chrom_list))]


				for p in as_completed(p_list):
					mtx_list, cell_id = p.result()
					for i in range(len(chrom_list)):
						mtx_all_list[i][cell_id] = mtx_list[i]

			else:
				data = pd.read_table(os.path.join(data_dir, "data.txt"), sep="\t")
				# ['cell_name','cell_id', 'chrom1', 'pos1', 'chrom2', 'pos2', 'count']
				cell_id_all = np.unique(data['cell_id'])
				cell_num = int(np.max(cell_id_all) + 1)
				bar = trange(cell_num)
				mtx_all_list = [[0] * cell_num for i in range(len(chrom_list))]
				p_list = []
				pool = ProcessPoolExecutor(max_workers=cpu_num)
				for cell_id in range(cell_num):
					p_list.append(pool.submit(data2mtx, config, data[data['cell_id'] == cell_id].reset_index(),
											  chrom_start_end, False, cell_id, blacklist))

				for p in as_completed(p_list):
					mtx_list, cell_id = p.result()
					for i in range(len(chrom_list)):
						mtx_all_list[i][cell_id] = mtx_list[i]
					bar.update(1)
				bar.close()
				pool.shutdown(wait=True)

		else:
			data = pd.read_table(os.path.join(data_dir, "data.txt"), sep="\t")
			cell_id_all = np.unique(data['cell_id'])
			cell_num = int(np.max(cell_id_all) + 1)
			bar = trange(cell_num)
			mtx_all_list = [[0] * cell_num for i in range(len(chrom_list))]
			p_list = []
			pool = ProcessPoolExecutor(max_workers=cpu_num)
			for cell_id in range(cell_num):
				p_list.append(
					pool.submit(data2mtx, config, data[data['cell_id'] == cell_id].reset_index(), chrom_start_end,
					            False, cell_id, blacklist))
			
			for p in as_completed(p_list):
				mtx_list, cell_id = p.result()
				for i in range(len(chrom_list)):
					mtx_all_list[i][cell_id] = mtx_list[i]
				bar.update(1)
			bar.close()
			pool.shutdown(wait=True)
		for i in range(len(chrom_list)):
			np.save(os.path.join(temp_dir, "raw", "%s_sparse_adj.npy" % chrom_list[i]), mtx_all_list[i],
			        allow_pickle=True)
	elif input_format == 'higashi_v2':
		print("extracting from filelist.txt")
		with open(os.path.join(data_dir, "filelist.txt"), "r") as f:
			lines = f.readlines()
			filelist = [line.strip() for line in lines]
		bar = trange(len(filelist))
		mtx_all_list = [[0] * len(filelist) for i in range(len(chrom_list))]
		p_list = []
		pool = ProcessPoolExecutor(max_workers=cpu_num)
		for cell_id, file in enumerate(filelist):
			p_list.append(pool.submit(data2mtx, config, file, chrom_start_end, False, cell_id, blacklist))
		
		for p in as_completed(p_list):
			mtx_list, cell_id = p.result()
			for i in range(len(chrom_list)):
				mtx_all_list[i][cell_id] = mtx_list[i]
			bar.update(1)
		bar.close()
		pool.shutdown(wait=True)
		
		for i in range(len(chrom_list)):
			np.save(os.path.join(temp_dir, "raw", "%s_sparse_adj.npy" % chrom_list[i]), mtx_all_list[i],
			        allow_pickle=True)
	else:
		print("invalid input format")
		raise EOFError
	
	
def remove_blacklist(blacklistbed, chromdf):
	import pybedtools
	blacklist = pybedtools.BedTool(blacklistbed)
	left = chromdf[['chrom1', 'pos1', 'pos1']].copy()
	left.loc[:, 'temp_indexname'] = np.arange(len(left))

	right = chromdf[['chrom2', 'pos2', 'pos2']].copy()
	right.loc[:, 'temp_indexname'] = np.arange(len(right))

	import tempfile
	f1 = tempfile.NamedTemporaryFile()
	left.to_csv(f1, sep="\t", header=False, index=False)
	bed_anchor = pybedtools.BedTool(f1.name)
	good_anchor = bed_anchor.subtract(blacklist)
	good_anchor_left = good_anchor.to_dataframe()

	f2 = tempfile.NamedTemporaryFile()
	right.to_csv(f2, sep="\t", header=False, index=False)
	bed_anchor = pybedtools.BedTool(f2.name)
	good_anchor = bed_anchor.subtract(blacklist)
	good_anchor_right = good_anchor.to_dataframe()

	good_index = np.intersect1d(good_anchor_left['name'], good_anchor_right['name'])
	ori_len = len(chromdf)
	# str1 = "length from %d " % len(chromdf)
	chromdf =  chromdf.iloc[good_index, :]
	# str1 += "to %d" % (len(chromdf))
	# if len(chromdf) < ori_len:
	# 	print (str1)
	pybedtools.helpers.cleanup()
	return chromdf


if __name__ == '__main__':
	args = parse_args()
	config = get_config(args.config)
	
	create_dir(config)
	generate_chrom_start_end(config)
	extract_table(config)