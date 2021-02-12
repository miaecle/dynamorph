import yaml
import logging


def log_warning(msg, *args, **kwargs):
    """Log message with level WARNING."""
    # import logging

    logging.getLogger(__name__).warning(msg, *args, **kwargs)


FILES = {
    'raw_dirs',
    'supp_dirs',
    'train_dirs',
    'val_dirs',
    'weights_dir'
}

PREPROCESS = {
    'cs',
    'cs_mask',
    'input_shape',
    'w_a',
    'w_t',
    'channel_mean',
    'channel_std'
}

INFERENCE = {
    'model',
    'weights',
    'gpus',
    'gpu_id',
    'fov',
    'channels',
    'num_classes',
    'window_size',
    'batch_size',
    'num_pred_rnd',
    'seg_val_cat'
}

TRAINING = {
    'model',
    'num_inputs',
    'num_hiddens',
    'num_residual_hiddens',
    'num_residual_layers',
    'num_embeddings',
    'commitment_cost',
    'alpha',
    'epochs',
    'learning_rate',
    'batch_size',
    'GPU',
    'GPU_ID',
    'shuffle_data',
    'transform',
}


class Object:
    pass


class YamlReader:

    files = Object()
    preprocess = Object()
    inference = Object()
    training = Object()

    def __init__(self):
        self.config = None

    def read_config(self, yml_config):
        with open(yml_config, 'r') as f:
            self.config = yaml.load(f)

            self._parse_files()
            self._parse_preprocessing()
            self._parse_inference()
            self._parse_training()

    def _parse_files(self):
        for key, value in self.config['files'].items():
            if key in FILES:
                self.files.key = value
            else:
                log_warning(f"yaml FILE config field {key} is not recognized")

    def _parse_preprocessing(self):
        for key, value in self.config['preprocess'].items():
            if key in PREPROCESS:
                self.preprocess.key = value
            else:
                log_warning(f"yaml PREPROCESS config field {key} is not recognized")

    def _parse_inference(self):
        for key, value in self.config['inference'].items():
            if key in INFERENCE:
                self.inference.key = value
            else:
                log_warning(f"yaml INFERENCE config field {key} is not recognized")

    def _parse_training(self):
        for key, value in self.config['training'].items():
            if key in TRAINING:
                self.training.key = value
            else:
                log_warning(f"yaml TRAINING config field {key} is not recognized")
