
default_model_parameters = {
    # Input (to UNet segmentation tool) normalization parameters
    "UNET_CHANNEL_MAX_PHASE": 65535,
    "UNET_CHANNEL_MAX_RETARDANCE": 65535,
    "UNET_MAXS": [65535, 65535], # Max intensities of each channel, required if using dynamorph UNet for training

    "UNET_CHANNEL_MEAN_PHASE": 32767,
    "UNET_CHANNEL_MEAN_RETARDANCE": 1500,
    "UNET_MEANS": [32767, 1500], # Mean intensities of each channel, required if using dynamorph UNet for training

    "UNET_CHANNEL_STD_PHASE": 1800,
    "UNET_CHANNEL_STD_RETARDANCE": 1700,
    "UNET_STDS": [1800, 1700], # SD of each channel, required if using dynamorph UNet for training

    # Patch extraction parameters
    "PATCH_WINDOW_SIZE": 256, # Size of single cell patch, required

    # Input (to VAE) normalization parameters
    "PATCH_MAX_PHASE": 65535,
    "PATCH_MAX_RETARDANCE": 65535,
    "PATCH_MAXS": [65535, 65535], # Max intensities of each channel, required

    "PATCH_MEAN_PHASE": 0.3960,
    "PATCH_MEAN_RETARDANCE": 0.0475,
    "PATCH_MEANS": [0.3960, 0.0475], # Mean (after scaled by PATCH_MAXS) of each channel, required

    "PATCH_STD_PHASE": 0.0514,
    "PATCH_STD_RETARDANCE": 0.0435,
    "PATCH_STDS": [0.0514, 0.0435], # SD (after scaled by PATCH_MAXS) of each channel, required

    # VAE model parameters
    "VAE_INPUT_SHAPE": (128, 128), # Size of patch input to VAE, if different from PATCH_WINDOW_SIZE patches will be resized
    "VAE_NUM_HIDDENS": 16, # number of hidden units in latent embeddings
    "VAE_NUM_EMBEDDINGS": 64, # number of VQ embedding vectors (used only in VQ-VAE)
    "VAE_NUM_RESIDUAL_HIDDENS": 32, # number of hidden units in the residual layer
    "VAE_NUM_RESIDUAL_LAYERS": 2, # number of residual layers
    "VAE_COMMITEMENT_COST": 0.25, # balance between latent losses (used only in VQ-VAE)
    "VAE_ALPHA": 0.005, # balance of matching loss (if applicable)
    "VAE_SAMPLING_K": 5, # number of sampling trials (used only in IWAE)

    # Model training hyper-parameters
    "TRAIN_N_EPOCHS": 10, 
    "TRAIN_LEARNING_RATE": 0.001, 
    "TRAIN_BATCH_SIZE": 16,
}

class DynaMorphConfig(object):

    def __init__(self):
        self._config = default_model_parameters

    def update(self, update_dict):
        for k in update_dict:
         self._config[k] = update_dict[k]

    def has_key(self, key):
        return key in self._config
    
    def keys(self):
        return self._config.keys()
    
    def __getitem__(self, key):
        if key in self._config:
            return self._config[key]
        else:
            print("Key %s not found in the config dict" % key)
            return None

    def __setitem__(self, key, value):
        self._config[key] = value
