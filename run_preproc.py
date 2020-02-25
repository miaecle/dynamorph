# bchhun, {2020-02-21}

# 1. check input: (n_frames * 2048 * 2048 * 2) channel 0 - phase, channel 1 - retardance
# 2. adjust channel range
#     a. phase: 32767 plus/minus 1600~2000
#     b. retardance: 1400~1600 plus/minus 1500~1800
# 3. save as '$SITE_NAME.npy' numpy array, dtype=uint16

from pipeline.preprocess import write_raw_to_npy

import os
from multiprocessing import Pool

# ESS from hulk
NOVEMBER = '/gpfs/CompMicro/Projects/learningCellState/microglia/20191107_1209_1_GW23/blank_bg_stabilized'
JANUARY = '/gpfs/CompMicro/Hummingbird/Processed/Galina/2020_01_28/SM_GW22_2020_0128_1404_1_SM_GW22_2020_0128_1404_1/blank_bg_stabilized'
JANUARY_FAST = '/gpfs/CompMicro/Hummingbird/Processed/Galina/2020_01_28/SM_GW22_2020_0128_1143_2hr_fastTimeSeries_1_SM_GW22_2020_0128_1143_2hr_fastTimeSeries_1/blank_bg_stabilized'


# SITES = ['B4-Site_0', 'B4-Site_1',  'B4-Site_2',  'B4-Site_3',  'B4-Site_4', 'B4-Site_5', 'B4-Site_6', 'B4-Site_7', 'B4-Site_8',
#          'B5-Site_0', 'B5-Site_1',  'B5-Site_2',  'B5-Site_3',  'B5-Site_4', 'B5-Site_5', 'B5-Site_6', 'B5-Site_7', 'B5-Site_8',
#          'C3-Site_0', 'C3-Site_1',  'C3-Site_2',  'C3-Site_3',  'C3-Site_4', 'C3-Site_5', 'C3-Site_6', 'C3-Site_7', 'C3-Site_8',
#          'C4-Site_0', 'C4-Site_1',  'C4-Site_2',  'C4-Site_3',  'C4-Site_4', 'C4-Site_5', 'C4-Site_6', 'C4-Site_7', 'C4-Site_8',
#          'C5-Site_0', 'C5-Site_1',  'C5-Site_2',  'C5-Site_3',  'C5-Site_4', 'C5-Site_5', 'C5-Site_6', 'C5-Site_7', 'C5-Site_8']


NOVEMBER_SITES = [
    'B2-Site_0', 'B2-Site_1',  'B2-Site_2',  'B2-Site_3',  'B2-Site_4', 'B2-Site_5', 'B2-Site_6', 'B2-Site_7', 'B2-Site_8',
    'B4-Site_0', 'B4-Site_1',  'B4-Site_2',  'B4-Site_3',  'B4-Site_4', 'B4-Site_5', 'B4-Site_6', 'B4-Site_7', 'B4-Site_8',
    'B5-Site_0', 'B5-Site_1',  'B5-Site_2',  'B5-Site_3',  'B5-Site_4', 'B5-Site_5', 'B5-Site_6', 'B5-Site_7', 'B5-Site_8',
    'C4-Site_0', 'C4-Site_1',  'C4-Site_2',  'C4-Site_3',  'C4-Site_4', 'C4-Site_5', 'C4-Site_6', 'C4-Site_7', 'C4-Site_8',
    'C5-Site_0', 'C5-Site_1',  'C5-Site_2',  'C5-Site_3',  'C5-Site_4', 'C5-Site_5', 'C5-Site_6', 'C5-Site_7', 'C5-Site_8']

# DATA_PREP = '/gpfs/CompMicro/Hummingbird/Processed/Galina/VAE/data_temp'
output = '/gpfs/CompMicro/Projects/learningCellState/microglia/raw_for_segmentation'


def main():

    for site in NOVEMBER_SITES:

        if not os.path.exists(output+os.sep+'NOVEMBER'):
            # os.mkdir(output+os.sep+'NOVEMBER')
            os.makedirs(output+os.sep+'NOVEMBER')

        out = output+os.sep+'NOVEMBER'

        print(f"writing {site} to {out}")
        write_raw_to_npy(NOVEMBER, site, out, multipage=True)


if __name__ == '__main__':
    main()
