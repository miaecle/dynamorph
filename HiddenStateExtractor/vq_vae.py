import os
import h5py
import cv2
import numpy as np
import scipy
import queue
import torch as t
import torch.nn as nn
import torch.nn.functional as F
import pickle
from torch.utils.data import TensorDataset, DataLoader
from scipy.sparse import csr_matrix

# Switch to root folder if import below doesn't work
from .naive_imagenet import read_file_path
from .generate_trajectory_relations import generate_trajectory_relations
from params import DynaMorphConfig

default_conf = DynaMorphConfig()
eps = 1e-9


class VectorQuantizer(nn.Module):
    """ Vector Quantizer module as introduced in 
        "Neural Discrete Representation Learning"

    This module contains a list of trainable embedding vectors, during training 
    and inference encodings of inputs will find their closest resemblance
    in this list, which will be reassembled as quantized encodings (decoder 
    input)

    """
    def __init__(self, embedding_dim=128, num_embeddings=128, commitment_cost=0.25, gpu=True):
        """ Initialize the module

        Args:
            embedding_dim (int, optional): size of embedding vector
            num_embeddings (int, optional): number of embedding vectors
            commitment_cost (float, optional): balance between latent losses
            gpu (bool, optional): if weights are saved on gpu

        """
        super(VectorQuantizer, self).__init__()
        self.embedding_dim = embedding_dim
        self.num_embeddings = num_embeddings
        self.commitment_cost = commitment_cost
        self.gpu = gpu
        self.w = nn.Embedding(num_embeddings, embedding_dim)

    def forward(self, inputs):
        """ Forward pass

        Args:
            inputs (torch tensor): encodings of input image

        Returns:
            torch tensor: quantized encodings (decoder input)
            torch tensor: quantization loss
            torch tensor: perplexity, measuring coverage of embedding vectors

        """
        # inputs: Batch * Num_hidden(=embedding_dim) * H * W
        distances = t.sum((inputs.unsqueeze(1) - self.w.weight.reshape((1, self.num_embeddings, self.embedding_dim, 1, 1)))**2, 2)

        # Decoder input
        encoding_indices = t.argmax(-distances, 1)
        quantized = self.w(encoding_indices).transpose(2, 3).transpose(1, 2)
        assert quantized.shape == inputs.shape
        output_quantized = inputs + (quantized - inputs).detach()

        # Commitment loss
        e_latent_loss = F.mse_loss(quantized.detach(), inputs)
        q_latent_loss = F.mse_loss(quantized, inputs.detach())
        loss = q_latent_loss + self.commitment_cost * e_latent_loss

        # Perplexity (used to monitor)
        # TODO: better deal with the gpu case here
        encoding_onehot = t.zeros(encoding_indices.flatten().shape[0], self.num_embeddings)
        if self.gpu:
            encoding_onehot = encoding_onehot.cuda()
        encoding_onehot.scatter_(1, encoding_indices.flatten().unsqueeze(1), 1)
        avg_probs = t.mean(encoding_onehot, 0)
        perplexity = t.exp(-t.sum(avg_probs*t.log(avg_probs + 1e-10)))

        return output_quantized, loss, perplexity

    @property
    def embeddings(self):
        return self.w.weight

    def encode_inputs(self, inputs):
        """ Find closest embedding vector combinations of input encodings

        Args:
            inputs (torch tensor): encodings of input image

        Returns:
            torch tensor: index tensor of embedding vectors
            
        """
        # inputs: Batch * Num_hidden(=embedding_dim) * H * W
        distances = t.sum((inputs.unsqueeze(1) - self.w.weight.reshape((1, self.num_embeddings, self.embedding_dim, 1, 1)))**2, 2)
        encoding_indices = t.argmax(-distances, 1)
        return encoding_indices

    def decode_inputs(self, encoding_indices):
        """ Assemble embedding vector index to quantized encodings

        Args:
            encoding_indices (torch tensor): index tensor of embedding vectors

        Returns:
            torch tensor: quantized encodings (decoder input)
            
        """
        quantized = self.w(encoding_indices).transpose(2, 3).transpose(1, 2)
        return quantized
      

class Reparametrize(nn.Module):
    """ Reparameterization step in RegularVAE
    """
    def forward(self, z_mean, z_logstd):
        """ Forward pass
        
        Args:
            z_mean (torch tensor): latent vector mean
            z_logstd (torch tensor): latent vector std (log)

        Returns:
            torch tensor: reparameterized latent vector
            torch tensor: KL divergence

        """
        z_std = t.exp(0.5 * z_logstd)
        eps = t.randn_like(z_std)
        z = z_mean + z_std * eps    
        KLD = -0.5 * t.sum(1 + z_logstd - z_mean.pow(2) - z_logstd.exp())
        return z, KLD


class Reparametrize_IW(nn.Module):
    """ Reparameterization step in IWAE
    """
    def __init__(self, k=5, **kwargs):
        """ Initialize the module

        Args:
            k (int, optional): number of sampling trials
            **kwargs: other keyword arguments

        """
        super(Reparametrize_IW, self).__init__(**kwargs)
        self.k = k
      
    def forward(self, z_mean, z_logstd):
        """ Forward pass
        
        Args:
            z_mean (torch tensor): latent vector mean
            z_logstd (torch tensor): latent vector std (log)

        Returns:
            torch tensor: reparameterized latent vectors
            torch tensor: randomness

        """
        z_std = t.exp(0.5 * z_logstd)
        epss = [t.randn_like(z_std) for _ in range(self.k)]
        zs = [z_mean + z_std * eps for eps in epss]
        return zs, epss


class Flatten(nn.Module):
    """ Helper module for flatten tensor
    """
    def forward(self, input):
        return input.view(input.size(0), -1)


class ResidualBlock(nn.Module):
    """ Customized residual block in network
    """
    def __init__(self,
                 num_hiddens=128,
                 num_residual_hiddens=512,
                 num_residual_layers=2):
        """ Initialize the module

        Args:
            num_hiddens (int, optional): number of hidden units
            num_residual_hiddens (int, optional): number of hidden units in the
                residual layer
            num_residual_layers (int, optional): number of residual layers

        """
        super(ResidualBlock, self).__init__()
        self.num_hiddens = num_hiddens
        self.num_residual_layers = num_residual_layers
        self.num_residual_hiddens = num_residual_hiddens

        self.layers = []
        for _ in range(self.num_residual_layers):
            self.layers.append(nn.Sequential(
                nn.ReLU(),
                nn.Conv2d(self.num_hiddens, self.num_residual_hiddens, 3, padding=1),
                nn.BatchNorm2d(self.num_residual_hiddens),
                nn.ReLU(),
                nn.Conv2d(self.num_residual_hiddens, self.num_hiddens, 1),
                nn.BatchNorm2d(self.num_hiddens)))
        self.layers = nn.ModuleList(self.layers)

    def forward(self, x):
        """ Forward pass

        Args:
            x (torch tensor): input tensor

        Returns:
            torch tensor: output tensor

        """
        output = x
        for i in range(self.num_residual_layers):
            output = output + self.layers[i](output)
        return output


class VQ_VAE(nn.Module):
    """ Vector-Quantized VAE as introduced in 
        "Neural Discrete Representation Learning"
    """
    def __init__(self,
                 params=default_conf,
                 gpu=True,
                 **kwargs):
        """ Initialize the model

        Args:
            params (DynaMorphConfig, optional): configuration dict, containing:
                VAE_NUM_HIDDENS (int): number of hidden units (size of latent 
                    encodings per position)
                VAE_NUM_RESIDUAL_HIDDENS (int): number of hidden units in the 
                    residual layer
                VAE_NUM_RESIDUAL_LAYERS (int): number of residual layers
                VAE_NUM_EMBEDDINGS (int): number of VQ embedding vectors
                VAE_COMMITEMENT_COST (float): balance between latent losses
                PATCH_STDS (list of float): each channel's SD, used 
                    for balancing loss across channels
                VAE_ALPHA (float): balance of matching loss
            gpu (bool, optional): if the model is run on gpu
            **kwargs: other keyword arguments

        """


        super(VQ_VAE, self).__init__(**kwargs)
        self.num_inputs = len(params['PATCH_STDS'])
        self.num_hiddens = params['VAE_NUM_HIDDENS']
        self.num_residual_layers = params['VAE_NUM_RESIDUAL_LAYERS']
        self.num_residual_hiddens = params['VAE_NUM_RESIDUAL_HIDDENS']
        self.num_embeddings = params['VAE_NUM_EMBEDDINGS']
        self.commitment_cost = params['VAE_COMMITEMENT_COST']
        self.channel_var = nn.Parameter(t.from_numpy(params['PATCH_STDS']).float().reshape((1, self.num_inputs, 1, 1)), requires_grad=False)
        self.alpha = params['VAE_ALPHA']

        self.enc = nn.Sequential(
            nn.Conv2d(self.num_inputs, self.num_hiddens//2, 1),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 3, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            ResidualBlock(self.num_hiddens, self.num_residual_hiddens, self.num_residual_layers))
        self.vq = VectorQuantizer(self.num_hiddens, self.num_embeddings, commitment_cost=self.commitment_cost, gpu=gpu)
        self.dec = nn.Sequential(
            nn.ConvTranspose2d(self.num_hiddens, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//2, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//4, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//4, self.num_inputs, 1))
      
    def forward(self, inputs, time_matching_mat=None, batch_mask=None):
        """ Forward pass

        Args:
            inputs (torch tensor): input cell image patches
            time_matching_mat (torch tensor or None, optional): if given, 
                pairwise relationship between samples in the minibatch, used 
                to calculate time matching loss
            batch_mask (torch tensor or None, optional): if given, weight mask 
                of training samples, used to concentrate loss on cell bodies

        Returns:
            torch tensor: decoded/reconstructed cell image patches
            dict: losses and perplexity of the minibatch

        """
        # inputs: Batch * num_inputs(channel) * H * W, each channel from 0 to 1
        z_before = self.enc(inputs)
        z_after, c_loss, perplexity = self.vq(z_before)
        decoded = self.dec(z_after)
        if batch_mask is None:
            batch_mask = t.ones_like(inputs)
        recon_loss = t.mean(F.mse_loss(decoded * batch_mask, inputs * batch_mask, reduction='none')/self.channel_var)
        total_loss = recon_loss + c_loss
        time_matching_loss = None
        if not time_matching_mat is None:
            z_before_ = z_before.reshape((z_before.shape[0], -1))
            len_latent = z_before_.shape[1]
            sim_mat = t.pow(z_before_.reshape((1, -1, len_latent)) - \
                            z_before_.reshape((-1, 1, len_latent)), 2).mean(2)
            assert sim_mat.shape == time_matching_mat.shape
            time_matching_loss = (sim_mat * time_matching_mat).sum()
            total_loss += time_matching_loss * self.alpha
        return decoded, \
               {'recon_loss': recon_loss,
                'commitment_loss': c_loss,
                'time_matching_loss': time_matching_loss,
                'total_loss': total_loss,
                'perplexity': perplexity}

    def predict(self, inputs):
        """ Prediction fn, same as forward pass """
        return self.forward(inputs)


class VAE(nn.Module):
    """ Regular VAE """
    def __init__(self,
                 params=default_conf,
                 **kwargs):
        """ Initialize the model

        Args:
            params (DynaMorphConfig, optional): configuration dict, containing:
                VAE_NUM_HIDDENS (int): number of hidden units (size of latent 
                    encodings per position)
                VAE_NUM_RESIDUAL_HIDDENS (int): number of hidden units in the 
                    residual layer
                VAE_NUM_RESIDUAL_LAYERS (int): number of residual layers
                PATCH_STDS (list of float): each channel's SD, used 
                    for balancing loss across channels
                VAE_ALPHA (float): balance of matching loss
            **kwargs: other keyword arguments

        """
        super(VAE, self).__init__(**kwargs)
        self.num_inputs = len(params['PATCH_STDS'])
        self.num_hiddens = params['VAE_NUM_HIDDENS']
        self.num_residual_layers = params['VAE_NUM_RESIDUAL_LAYERS']
        self.num_residual_hiddens = params['VAE_NUM_RESIDUAL_HIDDENS']
        self.channel_var = nn.Parameter(t.from_numpy(params['PATCH_STDS']).float().reshape((1, self.num_inputs, 1, 1)), requires_grad=False)
        self.alpha = params['VAE_ALPHA']

        self.enc = nn.Sequential(
            nn.Conv2d(self.num_inputs, self.num_hiddens//2, 1),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 3, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            ResidualBlock(self.num_hiddens, self.num_residual_hiddens, self.num_residual_layers),
            nn.Conv2d(self.num_hiddens, 2*self.num_hiddens, 1))
        self.rp = Reparametrize()
        self.dec = nn.Sequential(
            nn.ConvTranspose2d(self.num_hiddens, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//2, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//4, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//4, self.num_inputs, 1))
      
    def forward(self, inputs, time_matching_mat=None, batch_mask=None):
        """ Forward pass

        Args:
            inputs (torch tensor): input cell image patches
            time_matching_mat (torch tensor or None, optional): if given, 
                pairwise relationship between samples in the minibatch, used 
                to calculate time matching loss
            batch_mask (torch tensor or None, optional): if given, weight mask 
                of training samples, used to concentrate loss on cell bodies

        Returns:
            torch tensor: decoded/reconstructed cell image patches
            dict: losses and perplexity of the minibatch

        """
        # inputs: Batch * num_inputs(channel) * H * W, each channel from 0 to 1
        z_before = self.enc(inputs)
        z_mean = z_before[:, :self.num_hiddens]
        z_logstd = z_before[:, self.num_hiddens:]

        # Reparameterization trick
        z_after, KLD = self.rp(z_mean, z_logstd)
        
        decoded = self.dec(z_after)
        if batch_mask is None:
            batch_mask = t.ones_like(inputs)
        recon_loss = t.sum(F.mse_loss(decoded * batch_mask, inputs * batch_mask, reduction='none')/self.channel_var)
        total_loss = recon_loss + KLD
        time_matching_loss = None
        if not time_matching_mat is None:
            z_before_ = z_mean.reshape((z_mean.shape[0], -1))
            len_latent = z_before_.shape[1]
            sim_mat = t.pow(z_before_.reshape((1, -1, len_latent)) - \
                            z_before_.reshape((-1, 1, len_latent)), 2).mean(2)
            assert sim_mat.shape == time_matching_mat.shape
            time_matching_loss = (sim_mat * time_matching_mat).sum()
            total_loss += time_matching_loss * self.alpha
        return decoded, \
               {'recon_loss': recon_loss/(inputs.shape[0] * 32768),
                'KLD': KLD,
                'time_matching_loss': time_matching_loss,
                'total_loss': total_loss,
                'perplexity': t.zeros(())}

    def predict(self, inputs):
        """ Prediction fn without reparameterization

        Args:
            inputs (torch tensor): input cell image patches

        Returns:
            torch tensor: decoded/reconstructed cell image patches
            dict: reconstruction loss

        """
        # inputs: Batch * num_inputs(channel) * H * W, each channel from 0 to 1
        z_before = self.enc(inputs)
        z_mean = z_before[:, :self.num_hiddens]
        decoded = self.dec(z_mean)
        recon_loss = t.mean(F.mse_loss(decoded, inputs, reduction='none')/self.channel_var)
        return decoded, {'recon_loss': recon_loss}


class IWAE(VAE):
    """ Importance Weighted Autoencoder as introduced in 
        "Importance Weighted Autoencoders"
    """
    def __init__(self, params=default_conf, **kwargs):
        """ Initialize the model

        Args:
            params (DynaMorphConfig, optional): configuration dict, containing:
                All parameters required for VAE class
                VAE_SAMPLING_K (int): number of sampling trials
            **kwargs: other keyword arguments

        """
        super(IWAE, self).__init__(params=params, **kwargs)
        self.k = params["VAE_SAMPLING_K"]
        self.rp = Reparametrize_IW(k=self.k)

    def forward(self, inputs, time_matching_mat=None, batch_mask=None):
        """ Forward pass

        Args:
            inputs (torch tensor): input cell image patches
            time_matching_mat (torch tensor or None, optional): if given, 
                pairwise relationship between samples in the minibatch, used 
                to calculate time matching loss
            batch_mask (torch tensor or None, optional): if given, weight mask 
                of training samples, used to concentrate loss on cell bodies

        Returns:
            None: placeholder
            dict: losses and perplexity of the minibatch

        """
        z_before = self.enc(inputs)
        z_mean = z_before[:, :self.num_hiddens]
        z_logstd = z_before[:, self.num_hiddens:]
        z_afters, epss = self.rp(z_mean, z_logstd)

        if batch_mask is None:
            batch_mask = t.ones_like(inputs)
        time_matching_loss = 0.
        if not time_matching_mat is None:
            z_before_ = z_mean.reshape((z_mean.shape[0], -1))
            len_latent = z_before_.shape[1]
            sim_mat = t.pow(z_before_.reshape((1, -1, len_latent)) - \
                            z_before_.reshape((-1, 1, len_latent)), 2).mean(2)
            assert sim_mat.shape == time_matching_mat.shape
            time_matching_loss = (sim_mat * time_matching_mat).sum()

        log_ws = []
        recon_losses = []
        for z, eps in zip(z_afters, epss):
            decoded = self.dec(z)
            log_p_x_z = - t.sum(F.mse_loss(decoded * batch_mask, inputs * batch_mask, reduction='none')/self.channel_var, dim=(1, 2, 3))
            log_p_z = - t.sum(0.5 * z ** 2, dim=(1, 2, 3)) #- 0.5 * t.numel(z[0]) * np.log(2 * np.pi)
            log_q_z_x = - t.sum(0.5 * eps ** 2 + z_logstd, dim=(1, 2, 3)) #- 0.5 * t.numel(z[0]) * np.log(2 * np.pi) 
            log_w_unnormed = log_p_x_z  + log_p_z - log_q_z_x
            log_ws.append(log_w_unnormed)
            recon_losses.append(-log_p_x_z)
        log_ws = t.stack(log_ws, 1)
        log_ws_minus_max = log_ws - t.max(log_ws, dim=1, keepdim=True)[0]
        ws = t.exp(log_ws_minus_max)
        normalized_ws = ws / t.sum(ws, dim=1, keepdim=True)
        loss = -(normalized_ws.detach() * log_ws).sum()
        total_loss = loss + time_matching_loss
        
        recon_losses = t.stack(recon_losses, 1)
        recon_loss = (normalized_ws.detach() * recon_losses).sum()
        return None, \
               {'recon_loss': recon_loss/(inputs.shape[0] * 32768),
                'time_matching_loss': time_matching_loss,
                'total_loss': total_loss,
                'perplexity': t.zeros(())}


class AAE(nn.Module):
    """ Adversarial Autoencoder as introduced in 
        "Adversarial Autoencoders"
    """
    def __init__(self,
                 params=default_conf,
                 **kwargs):
        """ Initialize the model

        Args:
            params (DynaMorphConfig, optional): configuration dict, containing:
                VAE_NUM_HIDDENS (int): number of hidden units (size of latent 
                    encodings per position)
                VAE_NUM_RESIDUAL_HIDDENS (int): number of hidden units in the 
                    residual layer
                VAE_NUM_RESIDUAL_LAYERS (int): number of residual layers
                PATCH_STDS (list of float): each channel's SD, used 
                    for balancing loss across channels
                VAE_ALPHA (float): balance of matching loss
            **kwargs: other keyword arguments

        """
        super(AAE, self).__init__(**kwargs)
        self.num_inputs = len(params['PATCH_STDS'])
        self.num_hiddens = params['VAE_NUM_HIDDENS']
        self.num_residual_layers = params['VAE_NUM_RESIDUAL_LAYERS']
        self.num_residual_hiddens = params['VAE_NUM_RESIDUAL_HIDDENS']
        self.channel_var = nn.Parameter(t.from_numpy(params['PATCH_STDS']).float().reshape((1, self.num_inputs, 1, 1)), requires_grad=False)
        self.alpha = params['VAE_ALPHA']

        self.enc = nn.Sequential(
            nn.Conv2d(self.num_inputs, self.num_hiddens//2, 1),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens, self.num_hiddens, 3, padding=1),
            nn.BatchNorm2d(self.num_hiddens),
            ResidualBlock(self.num_hiddens, self.num_residual_hiddens, self.num_residual_layers))
        self.enc_d = nn.Sequential(
            nn.Conv2d(self.num_hiddens, self.num_hiddens//2, 1),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//2, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.BatchNorm2d(self.num_hiddens//2),
            nn.ReLU(),
            Flatten(),
            nn.Linear(self.num_hiddens * 2, self.num_hiddens * 8),
            nn.Dropout(0.25),
            nn.ReLU(),
            nn.Linear(self.num_hiddens * 8, self.num_hiddens),
            nn.Dropout(0.25),
            nn.ReLU(),
            nn.Linear(self.num_hiddens, 1),
            nn.Sigmoid())
        self.dec = nn.Sequential(
            nn.ConvTranspose2d(self.num_hiddens, self.num_hiddens//2, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//2, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(self.num_hiddens//4, self.num_hiddens//4, 4, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(self.num_hiddens//4, self.num_inputs, 1))
    
    def forward(self, inputs, time_matching_mat=None, batch_mask=None):
        """ Forward pass

        Args:
            inputs (torch tensor): input cell image patches
            time_matching_mat (torch tensor or None, optional): if given, 
                pairwise relationship between samples in the minibatch, used 
                to calculate time matching loss
            batch_mask (torch tensor or None, optional): if given, weight mask 
                of training samples, used to concentrate loss on cell bodies

        Returns:
            None: placeholder
            dict: losses and perplexity of the minibatch

        """
        # inputs: Batch * num_inputs(channel) * H * W, each channel from 0 to 1
        z = self.enc(inputs)
        decoded = self.dec(z)
        if batch_mask is None:
            batch_mask = t.ones_like(inputs)
        recon_loss = t.mean(F.mse_loss(decoded * batch_mask, inputs * batch_mask, reduction='none')/self.channel_var)
        total_loss = recon_loss
        time_matching_loss = None
        if not time_matching_mat is None:
            z_ = z.reshape((z.shape[0], -1))
            len_latent = z_.shape[1]
            sim_mat = t.pow(z_.reshape((1, -1, len_latent)) - \
                            z_.reshape((-1, 1, len_latent)), 2).mean(2)
            assert sim_mat.shape == time_matching_mat.shape
            time_matching_loss = (sim_mat * time_matching_mat).sum()
            total_loss += time_matching_loss * self.alpha
        return decoded, \
               {'recon_loss': recon_loss,
                'time_matching_loss': time_matching_loss,
                'total_loss': total_loss,
                'perplexity': t.zeros(())}

    def adversarial_loss(self, inputs):
        """ Calculate adversarial loss for the batch

        Args:
            inputs (torch tensor): input cell image patches

        Returns:
            dict: generator/discriminator losses

        """
        # inputs: Batch * num_inputs(channel) * H * W, each channel from 0 to 1
        z_data = self.enc(inputs)
        z_prior = t.randn_like(z_data)
        _z_data = self.enc_d(z_data)
        _z_prior = self.enc_d(z_prior)
        g_loss = -t.mean(t.log(_z_data + eps))
        d_loss = -t.mean(t.log(_z_prior + eps) + t.log(1 - _z_data.detach() + eps))
        return {'generator_loss': g_loss,
                'descriminator_loss': d_loss,
                'score': t.mean(_z_data)}

    def predict(self, inputs):
        """ Prediction fn, same as forward pass """
        return self.forward(inputs)



def train(model, 
          dataset, 
          relation_mat=None, 
          mask=None,
          params=default_conf,
          gpu=True):
    """ Train function for VQ-VAE, VAE, IWAE, etc.

    Args:
        model (nn.Module): autoencoder model
        dataset (TensorDataset): dataset of training inputs
        relation_mat (scipy csr matrix or None, optional): if given, sparse 
            matrix of pairwise relations
        mask (TensorDataset or None, optional): if given, dataset of training 
            sample weight masks
        params (DynaMorphConfig, optional): configuration dict, containing:
            TRAIN_N_EPOCHS (int): number of epochs
            TRAIN_LEARNING_RATE (float): learning rate
            TRAIN_BATCH_SIZE (int): batch size
            TRAIN_LOG_FILE (str or None): if given, path for training log file
        gpu (bool, optional): if the model is run on gpu
    
    Returns:
        nn.Module: trained model

    """

    n_epochs = params['TRAIN_N_EPOCHS']
    lr = params['TRAIN_LEARNING_RATE']
    batch_size = params['TRAIN_BATCH_SIZE']
    log_file = params['TRAIN_LOG_FILE']

    optimizer = t.optim.Adam(model.parameters(), lr=lr, betas=(.9, .999))
    model.zero_grad()

    n_batches = int(np.ceil(len(dataset)/batch_size))
    for epoch in range(n_epochs):
        recon_loss = []
        perplexities = []
        print('start epoch %d' % epoch) 
        for i in range(n_batches):
            # Input data
            batch = dataset[i*batch_size:(i+1)*batch_size][0]
            if gpu:
                batch = batch.cuda()
              
            # Relation (adjacent frame, same trajectory)
            if not relation_mat is None:
                batch_relation_mat = relation_mat[i*batch_size:(i+1)*batch_size, i*batch_size:(i+1)*batch_size]
                batch_relation_mat = batch_relation_mat.todense()
                batch_relation_mat = t.from_numpy(batch_relation_mat).float()
                if gpu:
                    batch_relation_mat = batch_relation_mat.cuda()
            else:
                batch_relation_mat = None
            
            # Reconstruction mask
            if not mask is None:
                batch_mask = mask[i*batch_size:(i+1)*batch_size][0][:, 1:2, :, :] # Hardcoded second slice (large mask)
                batch_mask = (batch_mask + 1.)/2.
                if gpu:
                    batch_mask = batch_mask.cuda()
            else:
                batch_mask = None
              
            _, loss_dict = model(batch, time_matching_mat=batch_relation_mat, batch_mask=batch_mask)
            loss_dict['total_loss'].backward()
            optimizer.step()
            model.zero_grad()

            recon_loss.append(loss_dict['recon_loss'])
            perplexities.append(loss_dict['perplexity'])
        log_str = 'epoch %d recon loss: %f perplexity: %f' % \
            (epoch, sum(recon_loss).item()/len(recon_loss), sum(perplexities).item()/len(perplexities))
        if log_file is not None:
            with open(log_file, 'a') as f:
                f.write(log_str + '\n')
        else:
            print(log_str)
    return model


def train_adversarial(model, 
                      dataset, 
                      relation_mat=None, 
                      mask=None, 
                      params=default_conf,
                      gpu=True):
    """ Train function for AAE.

    Args:
        model (nn.Module): autoencoder model (AAE)
        dataset (TensorDataset): dataset of training inputs
        relation_mat (scipy csr matrix or None, optional): if given, sparse 
            matrix of pairwise relations
        mask (TensorDataset or None, optional): if given, dataset of training 
            sample weight masks
        params (DynaMorphConfig, optional): configuration dict, containing:
            TRAIN_N_EPOCHS (int): number of epochs
            TRAIN_LEARNING_RATE (float): learning rate(s)
                if one float is given, then all learning rates (reconstruction, 
                discriminator, generator) are set to the same value; 
                if a list of 3 floats (respectively for reconstruction, 
                discriminator, generator) is given, then three learning rates 
                are set accordingly
            TRAIN_BATCH_SIZE (int): batch size
            TRAIN_LOG_FILE (str or None): if given, path for training log file
        gpu (bool, optional): if the model is run on gpu
    
    Returns:
        nn.Module: trained model

    """

    n_epochs = params['TRAIN_N_EPOCHS']
    lr = params['TRAIN_LEARNING_RATE']
    batch_size = params['TRAIN_BATCH_SIZE']
    log_file = params['TRAIN_LOG_FILE']

    if isinstance(lr, float):
        lr_recon = lr
        lr_dis = lr
        lr_gen = lr
    elif len(lr) == 3:
        lr_recon, lr_dis, lr_gen = lr

    optim_enc = t.optim.Adam(model.enc.parameters(), lr_recon)
    optim_dec = t.optim.Adam(model.dec.parameters(), lr_recon)
    optim_enc_g = t.optim.Adam(model.enc.parameters(), lr_gen)
    optim_enc_d = t.optim.Adam(model.enc_d.parameters(), lr_dis)
    model.zero_grad()

    n_batches = int(np.ceil(len(dataset)/batch_size))
    for epoch in range(n_epochs):
        recon_loss = []
        scores = []
        print('start epoch %d' % epoch) 
        for i in range(n_batches):
            # Input data
            batch = dataset[i*batch_size:(i+1)*batch_size][0]
            if gpu:
                batch = batch.cuda()
              
            # Relation (trajectory, adjacent)
            if not relation_mat is None:
                batch_relation_mat = relation_mat[i*batch_size:(i+1)*batch_size, i*batch_size:(i+1)*batch_size]
                batch_relation_mat = batch_relation_mat.todense()
                batch_relation_mat = t.from_numpy(batch_relation_mat).float()
                if gpu:
                    batch_relation_mat = batch_relation_mat.cuda()
            else:
                batch_relation_mat = None
            
            # Reconstruction mask
            if not mask is None:
                batch_mask = mask[i*batch_size:(i+1)*batch_size][0][:, 1:2, :, :] # Hardcoded second slice (large mask)
                batch_mask = (batch_mask + 1.)/2.
                if gpu:
                    batch_mask = batch_mask.cuda()
            else:
                batch_mask = None
              
            _, loss_dict = model(batch, time_matching_mat=batch_relation_mat, batch_mask=batch_mask)
            loss_dict['total_loss'].backward()
            optim_enc.step()
            optim_dec.step()
            loss_dict2 = model.adversarial_loss(batch)
            loss_dict2['descriminator_loss'].backward()
            optim_enc_d.step()
            loss_dict2['generator_loss'].backward()
            optim_enc_g.step()
            model.zero_grad()

            recon_loss.append(loss_dict['recon_loss'])
            scores.append(loss_dict2['score'])
        log_str = 'epoch %d recon loss: %f pred score: %f' % (epoch, sum(recon_loss).item()/len(recon_loss), sum(scores).item()/len(scores))
        if log_file is not None:
            with open(log_file, 'a') as f:
                f.write(log_str + '\n')
        else:
            print(log_str)
    return model


def normalize_dataset(combined_tensor,
                      normalize_param=default_conf):
    """ Normalize each channel of the dataset

    Args:
        combined_tensor (Tensor): combined dataset in torch.Tensor form
            Channel-first structure
        normalize_param (dict or DynaMorphConfig, optional): 
            dict containing normalization parameters

    Returns:
        Tensor: combined dataset tensor after normalization

    """
    channel_means = normalize_param['PATCH_MEANS']
    channel_stds = normalize_param['PATCH_STDS']
    assert len(channel_means) == len(channel_stds) == combined_tensor.shape[1]
    normalized_slices = []
    for i_slice in range(len(channel_means)):
        slic = combined_tensor[:, i_slice]
        slic = (slic - slic.mean()) / slic.std()
        slic = slic * channel_stds[i_slice] + channel_means[i_slice]
        slic = slic.clamp(min=0.0, max=1.0)
        normalized_slices.append(slic)
    return t.stack(normalized_slices, 1)


def prepare_dataset(fs,
                    cs=[0, 1], 
                    input_shape=default_conf['VAE_INPUT_SHAPE'], 
                    channel_max=default_conf['PATCH_MAXS'],
                    normalize=False,
                    normalize_param=default_conf):
    """ Prepare input dataset for VAE

    This function reads individual h5 files

    Args:
        fs (list of str): list of file paths/single cell patch identifiers, 
            images are saved as individual h5 files
        cs (list of int, optional): channels in the input
        input_shape (tuple, optional): input shape (height and width only)
        channel_max (np.array, optional): max intensities for channels
        normalize (bool, optional): if to normalize each channel
        normalize_param (dict or DynaMorphConfig, optional): 
            dict containing normalization parameters


    Returns:
        TensorDataset: dataset of training inputs

    """
    tensors = []
    for i, f_n in enumerate(fs):
      if i%1000 == 0:
        print("Processed %d" % i)
      with h5py.File(f_n, 'r') as f:
        dat = f['masked_mat']
        if cs is None:
          cs = np.arange(dat.shape[2])
        stacks = []
        for c, m in zip(cs, channel_max):
          c_slice = cv2.resize(np.array(dat[:, :, c]).astype(float), input_shape)
          stacks.append(c_slice/m)
        tensors.append(t.from_numpy(np.stack(stacks, 0)).float())

    combined_tensor = t.stack(tensors, 0)
    if normalize:
        combined_tensor = normalize_dataset(combined_tensor, normalize_param)
    dataset = TensorDataset(combined_tensor)
    return dataset


def prepare_dataset_from_collection(fs, 
                                    cs=[0, 1], 
                                    input_shape=default_conf['VAE_INPUT_SHAPE'], 
                                    channel_max=default_conf['PATCH_MAXS'],
                                    normalize=False,
                                    normalize_param=default_conf,
                                    file_path='./',
                                    file_suffix='_all_patches.pkl'):
    """ Prepare input dataset for VAE, deprecated

    This function reads assembled pickle files (dict)

    Args:
        fs (list of str): list of pickle file names
        cs (list of int, optional): channels in the input
        input_shape (tuple, optional): input shape (height and width only)
        channel_max (np.array, optional): max intensities for channels
        normalize (bool, optional): if to normalize each channel
        normalize_param (dict or DynaMorphConfig, optional): 
            dict containing normalization parameters
        file_path (str, optional): root folder for saved pickle files
        file_suffix (str, optional): suffix of saved pickle files

    Returns:
        TensorDataset: dataset of training inputs

    """
    assert len(cs) == len(channel_max)

    tensors = {}
    files = set([f.split('/')[-2] for f in fs])
    for file_name in files:
        file_dat = pickle.load(open(os.path.join(file_path, '%s%s' % (file_name, file_suffix)), 'rb')) #HARDCODED
        fs_ = [f for f in fs if f.split('/')[-2] == file_name ]
        for i, f_n in enumerate(fs_):
            dat = file_dat[f_n]['masked_mat']
            if cs is None:
                cs = np.arange(dat.shape[2])
            stacks = []
            for c, m in zip(cs, channel_max):
                c_slice = cv2.resize(np.array(dat[:, :, c]).astype(float), input_shape)
                stacks.append(c_slice/m)
            tensors[f_n] = t.from_numpy(np.stack(stacks, 0)).float()

    combined_tensor = t.stack([tensors[f_n] for f_n in fs], 0)
    if normalize:
        combined_tensor = normalize_dataset(combined_tensor, normalize_param)
    dataset = TensorDataset(combined_tensor)
    return dataset


def prepare_dataset_v2(dat_fs, 
                       cs=[0, 1],
                       input_shape=default_conf['VAE_INPUT_SHAPE'], 
                       channel_max=default_conf['PATCH_MAXS'],
                       normalize=False,
                       normalize_param=default_conf):
    """ Prepare input dataset for VAE

    This function reads assembled pickle files (dict)

    Args:
        dat_fs (list of str): list of pickle file paths
        cs (list of int, optional): channels in the input
        input_shape (tuple, optional): input shape (height and width only)
        channel_max (np.array, optional): max intensities for channels
        normalize (bool, optional): if to normalize each channel
        normalize_param (dict or DynaMorphConfig, optional): 
            dict containing normalization parameters

    Returns:
        TensorDataset: dataset of training inputs
        list of str: identifiers of single cell image patches

    """
    assert len(cs) == len(channel_max)
    tensors = {}
    for dat_f in dat_fs:
        print(f"\tloading data {dat_f}")
        file_dats = pickle.load(open(dat_f, 'rb'))
        for k in file_dats:
            dat = file_dats[k]['masked_mat']
            if cs is None:
                cs = np.arange(dat.shape[2])
            stacks = []
            for c, m in zip(cs, channel_max):
                c_slice = cv2.resize(np.array(dat[:, :, c]).astype(float), input_shape)
                stacks.append(c_slice/m)
            tensors[k] = t.from_numpy(np.stack(stacks, 0)).float()
    fs = sorted(tensors.keys())

    combined_tensor = t.stack([tensors[f_n] for f_n in fs], 0)
    if normalize:
        combined_tensor = normalize_dataset(combined_tensor, normalize_param)
    dataset = TensorDataset(combined_tensor)
    return dataset, fs


def reorder_with_trajectories(dataset, relations, seed=None):
    """ Reorder `dataset` to facilitate training with matching loss

    Args:
        dataset (TensorDataset): dataset of training inputs
        relations (dict): dict of pairwise relationship (adjacent frames, same 
            trajectory)
        seed (int or None, optional): if given, random seed

    Returns:
        TensorDataset: dataset of training inputs (after reordering)
        scipy csr matrix: sparse matrix of pairwise relations
        list of int: index of samples used for reordering

    """
    if not seed is None:
        np.random.seed(seed)
    inds_pool = set(range(len(dataset)))
    inds_in_order = []
    relation_dict = {}
    for pair in relations:
        if relations[pair] == 2: # Adjacent pairs
            if pair[0] not in relation_dict:
                relation_dict[pair[0]] = []
            relation_dict[pair[0]].append(pair[1])
    while len(inds_pool) > 0:
        rand_ind = np.random.choice(list(inds_pool))
        if not rand_ind in relation_dict:
            inds_in_order.append(rand_ind)
            inds_pool.remove(rand_ind)
        else:
            traj = [rand_ind]
            q = queue.Queue()
            q.put(rand_ind)
            while True:
                try:
                    elem = q.get_nowait()
                except queue.Empty:
                    break
                new_elems = relation_dict[elem]
                for e in new_elems:
                    if not e in traj:
                        traj.append(e)
                        q.put(e)
            inds_in_order.extend(traj)
            for e in traj:
                inds_pool.remove(e)
    new_tensor = dataset.tensors[0][np.array(inds_in_order)]
    
    values = []
    new_relations = []
    for k, v in relations.items():
        # 2 - adjacent, 1 - same trajectory
        if v == 1:
            values.append(0.1)
        elif v == 2:
            values.append(1.1)
        new_relations.append(k)
    new_relations = np.array(new_relations)
    relation_mat = csr_matrix((np.array(values), (new_relations[:, 0], new_relations[:, 1])),
                              shape=(len(dataset), len(dataset)))
    relation_mat = relation_mat[np.array(inds_in_order)][:, np.array(inds_in_order)]
    return TensorDataset(new_tensor), relation_mat, inds_in_order


def rescale(dataset):
    """ (deprecated) Rescale value range of image patches in `dataset` to CHANNEL_RANGE

    Deprecated, this function is now combined into prepare dataset methods

    Args:
        dataset (TensorDataset): dataset before rescaling

    Returns:
        TensorDataset: dataset after rescaling

    """
    CHANNEL_RANGE = [(0.3, 0.8), (0., 0.6)] 
    tensor = dataset.tensors[0]
    assert len(CHANNEL_RANGE) == tensor.shape[1]
    channel_slices = []
    for i in range(len(CHANNEL_RANGE)):
        lower_, upper_ = CHANNEL_RANGE[i]
        channel_slice = (tensor[:, i] - lower_) / (upper_ - lower_)
        channel_slice = t.clamp(channel_slice, 0, 1)
        channel_slices.append(channel_slice)
    new_tensor = t.stack(channel_slices, 1)
    return TensorDataset(new_tensor)


def resscale_backward(tensor):
    """ (deprecated) Reverse operation of `rescale`

    Args:
        dataset (TensorDataset): dataset after rescaling

    Returns:
        TensorDataset: dataset before rescaling

    """
    CHANNEL_RANGE = [(0.3, 0.8), (0., 0.6)] 
    assert len(tensor.shape) == 4
    assert len(CHANNEL_RANGE) == tensor.shape[1]
    channel_slices = []
    for i in range(len(CHANNEL_RANGE)):
        lower_, upper_ = CHANNEL_RANGE[i]
        channel_slice = lower_ + tensor[:, i] * (upper_ - lower_)
        channel_slices.append(channel_slice)
    new_tensor = t.stack(channel_slices, 1)
    return new_tensor


if __name__ == '__main__':
    """ Script below requires individual cell patches and their segmentation masks
    
    Starting from DynaMorph pipeline step 7
    """

    ### Settings ###
    cs = [0, 1]
    cs_mask = [2, 3]
    gpu = True
    config = DynaMorphConfig()

    summary_folder = None
    supp_folder = None
    sites = []

    ### Prepare Data ###
    wells = set(s[:2] for s in sites)
    well_data = {}
    for well in wells:
        well_sites = [s for s in sites if s[:2] == well]
        dat_fs = []
        for site in well_sites:
            supp_files_folder = os.path.join(supp_folder, '%s-supps' % well, '%s' % site)
            dat_fs.extend([os.path.join(supp_files_folder, f) \
                for f in os.listdir(supp_files_folder) if f.startswith('stacks')])
        dataset, fs = prepare_dataset_v2(dat_fs, 
                                         cs=cs, 
                                         input_shape=config['VAE_INPUT_SHAPE'], 
                                         channel_max=config['PATCH_MAXS'],
                                         normalize=True,
                                         normalize_param=config)
        dataset_mask, fs2 = prepare_dataset_v2(dat_fs, 
                                               cs=cs_mask, 
                                               input_shape=config['VAE_INPUT_SHAPE'], 
                                               channel_max=[1., 1.],
                                               normalize=False)
        assert fs == fs2
        assert fs == sorted(fs)

        t.save(dataset, os.path.join(summary_folder, '%s_adjusted_static_patches.pt' % well))
        t.save(dataset_mask, os.path.join(summary_folder, '%s_static_patches_mask.pt' % well))
        with open(os.path.join(summary_folder, '%s_file_paths.pkl' % well), 'wb') as f:
            pickle.dump(fs, f)

        relations = generate_trajectory_relations(well_sites, summary_folder, supp_folder)

        # dataset = t.load(os.path.join(summary_folder, '%s_adjusted_static_patches.pt' % well))
        # dataset_mask = t.load(os.path.join(summary_folder, '%s_static_patches_mask.pt' % well))
        # fs = pickle.load(open(os.path.join(summary_folder, "%s_file_paths.pkl" % well), 'rb'))
        # relations = pickle.load(open(os.path.join(summary_folder, "%s_static_patches_relations.pkl" % well), 'rb'))

        well_data[well] = (dataset, dataset_mask, fs, relations)


    ### Merge Training Data ###
    merged_tensor = []
    merged_mask_tensor = []
    merged_fs = []
    cumsum = 0
    merged_relations = {}

    for well in sorted(well_data):
        dataset, dataset_mask, fs, relations = well_data[well]
        merged_tensor.append(dataset.tensors[0])
        merged_mask_tensor.append(dataset_mask.tensors[0])
        merged_fs.extend(fs)
        for k in relations:
            merged_relations[(k[0] + cumsum, k[1] + cumsum)] = relations[k]
        cumsum += len(fs)

    merged_tensor = t.cat(merged_tensor, 0)
    merged_mask_tensor = t.cat(merged_mask_tensor, 0)
    merged_dataset = TensorDataset(merged_tensor)
    merged_dataset, relation_mat, inds_in_order = reorder_with_trajectories(
        merged_dataset, merged_relations, seed=123)
    merged_dataset_mask = TensorDataset(merged_mask_tensor[np.array(inds_in_order)])
    

    ### Initialize Model ###
    model = VQ_VAE(params=config, gpu=gpu)
    if gpu:
        model = model.cuda()


    ### Model Training ###
    model = train(model, 
                  merged_dataset, 
                  relation_mat=relation_mat, 
                  mask=merged_dataset_mask,
                  params=config,
                  gpu=gpu)
    t.save(model.state_dict(), 'temp.pt')



    """ Some helper functions below """

    ### Check coverage of embedding vectors ###
    used_indices = []
    for i in range(500):
        sample = dataset[i:(i+1)][0].cuda()
        z_before = model.enc(sample)
        indices = model.vq.encode_inputs(z_before)
        used_indices.append(np.unique(indices.cpu().data.numpy()))
    print(np.unique(np.concatenate(used_indices)))


    ### Generate latent vectors ###
    z_bs = {}
    z_as = {}
    for i in range(len(dataset)):
        sample = dataset[i:(i+1)][0].cuda()
        z_b = model.enc(sample)
        z_a, _, _ = model.vq(z_b)

        f_n = fs[inds_in_order[i]]
        z_as[f_n] = z_a.cpu().data.numpy()
        z_bs[f_n] = z_b.cpu().data.numpy()
      
    
    ### Visualize reconstruction ###
    def enhance(mat, lower_thr, upper_thr):
        mat = np.clip(mat, lower_thr, upper_thr)
        mat = (mat - lower_thr)/(upper_thr - lower_thr)
        return mat

    random_inds = np.random.randint(0, len(dataset), (10,))
    for i in random_inds:
        sample = dataset[i:(i+1)][0].cuda()
        cv2.imwrite('sample%d_0.png' % i, 
            enhance(sample[0, 0].cpu().data.numpy(), 0., 1.)*255)
        cv2.imwrite('sample%d_1.png' % i, 
            enhance(sample[0, 1].cpu().data.numpy(), 0., 1.)*255)
        output = model(sample)[0]
        cv2.imwrite('sample%d_0_rebuilt.png' % i, 
            enhance(output[0, 0].cpu().data.numpy(), 0., 1.)*255)
        cv2.imwrite('sample%d_1_rebuilt.png' % i, 
            enhance(output[0, 1].cpu().data.numpy(), 0., 1.)*255)