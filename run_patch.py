# bchhun, {2020-02-21}

from pipeline.patch_VAE import extract_patches, build_trajectories
from multiprocessing import Pool, Queue, Process
import os
import numpy as np
import argparse
import json
from params import DynaMorphConfig


class Worker(Process):
    def __init__(self, inputs, params, gpuid=0, method='extract_patches'):
        super().__init__()
        self.gpuid = gpuid
        self.inputs = inputs
        self.params = params
        self.method = method

    def run(self):
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = str(self.gpuid)

        if self.method == 'extract_patches':
            extract_patches(self.inputs, self.params)
        elif self.method == 'build_trajectories':
            build_trajectories(self.inputs, self.params)


def main(arguments_):

    # Configurations
    params = DynaMorphConfig()
    if arguments_.param:
        extra_params_dict = json.load(open(arguments_.param, "r"))
        params.update(extra_params_dict)
        for k in extra_params_dict:
            print("Using parameter %s: %s" % (str(k), str(extra_params_dict[k])))

    print("CLI arguments provided")
    raw = arguments_.raw
    supp = arguments_.supplementary

    n_gpu = arguments_.gpus
    method = arguments_.method

    # extract patches needs raw (NN probs, stack), supp (cell_positions, cell_pixel_assignments)
    if arguments_.method == 'extract_patches':
        if not arguments_.raw:
            raise AttributeError("raw directory must be specified when method = extract_patches")
        if not arguments_.supplementary:
            raise AttributeError("supplementary directory must be specified when method = extract_patches")

    # extract patches needs supp (cell_positions, cell_pixel_assignments)
    elif arguments_.method == 'build_trajectories':
        if not arguments_.supplementary:
            raise AttributeError("supplementary directory must be specified when method = extract_patches")

    if arguments_.fov:
        sites = arguments_.fov
    else:
        sites = [site for site in os.listdir(raw) if os.path.isdir(os.path.join(raw, site))]

    # if probabilities and formatted stack exist
    segment_sites = [site for site in sites if os.path.exists(os.path.join(raw, "%s.npy" % site)) and \
                     os.path.exists(os.path.join(raw, "%s_NNProbabilities.npy" % site))]
    if len(segment_sites) == 0:
        raise AttributeError("no sites found in raw directory with preprocessed data and matching NNProbabilities")

    # process each site on a different GPU if using multi-gpu
    sep = np.linspace(0, len(segment_sites), n_gpu + 1).astype(int)

    # TARGET is never used in either extract_patches or build_trajectory
    processes = []
    for i in range(n_gpu):
        _sites = segment_sites[sep[i]:sep[i + 1]]
        args = (raw, supp, None, _sites)
        p = Worker(args, params, gpuid=i, method=method)
        p.start()
        processes.append(p)
    for p in processes:
        p.join()


def parse_args():
    """
    Parse command line arguments for CLI.

    :return: namespace containing the arguments passed.
    """
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-r', '--raw',
        type=str,
        required=False,
        help="Path to the folder for raw inputs (multipage-tiff file of format [t, x, y]) and summary results",
    )
    parser.add_argument(
        '-s', '--supplementary',
        type=str,
        required=False,
        help="Path to the folder for supplementary results",
    )
    parser.add_argument(
        '-m', '--method',
        type=str,
        required=True,
        choices=['extract_patches', 'build_trajectories'],
        default='extract_patches',
        help="Method: one of 'extract_patches', 'build_trajectories'",
    )
    parser.add_argument(
        '-g', '--gpus',
        type=int,
        required=False,
        default=1,
        help="Number of GPS to use",
    )
    parser.add_argument(
        '-f', '--fov',
        type=lambda s: [str(item.strip(' ').strip("'")) for item in s.split(',')],
        required=False,
        help="list of field-of-views to process (subfolders in raw data directory)",
    )
    parser.add_argument(
        '-p', '--param',
        type=str,
        required=False,
        help="Path to the json file for configuration",
    )

    return parser.parse_args()


if __name__ == '__main__':
    arguments = parse_args()
    main(arguments)
