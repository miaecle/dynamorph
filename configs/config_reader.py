import yaml
import logging


def log_warning(msg, *args, **kwargs):
    """Log message with level WARNING."""
    # import logging

    logging.getLogger(__name__).warning(msg, *args, **kwargs)


class YamlReader:

    files = {
        'raw_dirs',
        'supp_dirs',
        'train_dirs',
        'model_dir'
    }

    preprocess = {
        'cs',
        'cs_mask',
        'input_shape',
        'w_a',
        'w_t',
        'channel_mean',
        'channel_std'
    }

    model = {
        'model',
        'num_inputs',
        'num_hiddens',
        'num_residual_hiddens',
        'num_residual_layers',
        'num_embeddings',
        'commitment_cost',
        'alpha',
    }

    training = {
        'epochs',
        'learning_rate',
        'batch_size',
        'GPU',
        'GPU_ID',
        'shuffle_data',
        'transform',
    }

    def __init__(self):
        self.config = None
        self._check_overlapping_attr()

    def read_config(self, yml_config):
        with open(yml_config, 'r') as f:
            self.config = yaml.load(f)

            self._parse_files()
            self._parse_trajectory_relations()
            self._parse_model()
            self._parse_training()

    def _check_overlapping_attr(self):
        if len(self.files & self.preprocess & self.model & self.training) != 0:
            raise AttributeError("repeated names for pre-defined configuration attributes,"
                                 "all attributes must be unique names")

    def _parse_files(self):
        for key, value in self.config['files'].items():
            if key in self.files:
                self.__setattr__(key, value)
            else:
                log_warning(f"yaml FILE config field {key} is not recognized")

    def _parse_trajectory_relations(self):
        for key, value in self.config['trajectory_relations'].items():
            if key in self.files:
                self.__setattr__(key, value)
            else:
                log_warning(f"yaml TRAJECTORY config field {key} is not recognized")

    def _parse_model(self):
        for key, value in self.config['model'].items():
            if key in self.files:
                self.__setattr__(key, value)
            else:
                log_warning(f"yaml MODEL config field {key} is not recognized")

    def _parse_training(self):
        for key, value in self.config['training'].items():
            if key in self.files:
                self.__setattr__(key, value)
            else:
                log_warning(f"yaml TRAINING config field {key} is not recognized")
