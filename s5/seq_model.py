import jax
import jax.numpy as np
from flax import linen as nn
from .layers import SequenceLayer
from .utils.util import SimpleMLP
from typing import Callable


class StackedEncoderModel(nn.Module):
    """ Defines a stack of S5 layers to be used as an encoder.
        Args:
            ssm         (nn.Module): the SSM to be used (i.e. S5 ssm)
            d_model     (int32):    this is the feature size of the layer inputs and outputs
                                     we usually refer to this size as H
            n_layers    (int32):    the number of S5 layers to stack
            activation  (string):   Type of activation function to use
            dropout     (float32):  dropout rate
            training    (bool):     whether in training mode or not
            prenorm     (bool):     apply prenorm if true or postnorm if false
            batchnorm   (bool):     apply batchnorm if true or layernorm if false
            bn_momentum (float32):  the batchnorm momentum if batchnorm is used
            step_rescale  (float32):  allows for uniformly changing the timescale parameter,
                                    e.g. after training on a different resolution for
                                    the speech commands benchmark
    """
    ssm: nn.Module
    d_model: int
    n_layers: int
    activation: str = "gelu"
    dropout: float = 0.0
    training: bool = True
    prenorm: bool = False
    batchnorm: bool = False
    bn_momentum: float = 0.9
    step_rescale: float = 1.0

    append_integration_timestep: bool = False
    use_integration_timestep: bool = True
    encoder_fn: Callable = nn.Dense

    def setup(self):
        """
        Initializes a linear encoder and the stack of S5 layers.
        """

        self.encoder = self.encoder_fn(self.d_model)
        # self.encoder = SimpleMLP([self.d_model, self.d_model])

        self.layers = [
            SequenceLayer(
                ssm=self.ssm,
                dropout=self.dropout,
                d_model=self.d_model,
                activation=self.activation,
                training=self.training,
                prenorm=self.prenorm,
                batchnorm=self.batchnorm,
                bn_momentum=self.bn_momentum,
                step_rescale=self.step_rescale,
            )
            for _ in range(self.n_layers)
        ]

    def __call__(self, x, training, integration_timesteps):
        """
        Compute the LxH output of the stacked encoder given an Lxd_input
        input sequence.
        Args:
             x (float32): input sequence (L, d_input)
        Returns:
            output sequence (float32): (L, d_model)
        """

        if integration_timesteps is None:
            x = self.encoder(x)
        else:
            x = self.encoder(x, integration_timesteps)

            # TODO -- need to handle the variable integration timesteps here.

            # There are three ways that we handle the integration timestep.
            if (not self.use_integration_timestep) or self.append_integration_timestep:
                # If we aren't using the integration timesteps, then make them equal one.
                # integration_timesteps = 1.0 + (integration_timesteps * 0.0)
                integration_timesteps = None  # TODO - Alternative method for discarding integration timesteps.
                print('\n\nWarning:  discarding/appending integration timesteps. \n\n')

        for layer in self.layers:
            x = layer(x, training, integration_timesteps)
        return x


def masked_meanpool(x, lengths):
    """
    Helper function to perform mean pooling across the sequence length
    when sequences have variable lengths. We only want to pool across
    the prepadded sequence length.
    Args:
         x (float32): input sequence (L, d_model)
         lengths (int32):   the original length of the sequence before padding
    Returns:
        mean pooled output sequence (float32): (d_model)
    """
    L = x.shape[0]
    mask = np.arange(L) < lengths
    return np.sum(mask[..., None]*x, axis=0)/lengths


# Here we call vmap to parallelize across a batch of input sequences
batch_masked_meanpool = jax.vmap(masked_meanpool)


class ClassificationModel(nn.Module):
    """ S5 classificaton sequence model. This consists of the stacked encoder
    (which consists of a linear encoder and stack of S5 layers), mean pooling
    across the sequence length, a linear decoder, and a softmax operation.
        Args:
            ssm         (nn.Module): the SSM to be used (i.e. S5 ssm)
            d_output     (int32):    the output dimension, i.e. the number of classes
            d_model     (int32):    this is the feature size of the layer inputs and outputs
                        we usually refer to this size as H
            n_layers    (int32):    the number of S5 layers to stack
            padded:     (bool):     if true: padding was used
            activation  (string):   Type of activation function to use
            dropout     (float32):  dropout rate
            training    (bool):     whether in training mode or not
            mode        (str):      Options: [pool: use mean pooling, last: just take
                                                                       the last state]
            prenorm     (bool):     apply prenorm if true or postnorm if false
            batchnorm   (bool):     apply batchnorm if true or layernorm if false
            bn_momentum (float32):  the batchnorm momentum if batchnorm is used
            step_rescale  (float32):  allows for uniformly changing the timescale parameter,
                                    e.g. after training on a different resolution for
                                    the speech commands benchmark
    """
    ssm: nn.Module
    d_output: int
    d_model: int
    n_layers: int
    padded: bool
    activation: str = "gelu"
    dropout: float = 0.2
    training: bool = True
    mode: str = "pool"
    prenorm: bool = False
    batchnorm: bool = False
    bn_momentum: float = 0.9
    step_rescale: float = 1.0

    def setup(self):
        """
        Initializes the S5 stacked encoder and a linear decoder.
        """
        self.encoder = StackedEncoderModel(
                            ssm=self.ssm,
                            d_model=self.d_model,
                            n_layers=self.n_layers,
                            activation=self.activation,
                            dropout=self.dropout,
                            training=self.training,
                            prenorm=self.prenorm,
                            batchnorm=self.batchnorm,
                            bn_momentum=self.bn_momentum,
                            step_rescale=self.step_rescale,
                                        )
        self.decoder = nn.Dense(self.d_output)

    def __call__(self, x, integration_timesteps):
        """
        Compute the size d_output log softmax output given a
        Lxd_input input sequence.
        Args:
             x (float32): input sequence (L, d_input)
        Returns:
            output (float32): (d_output)
        """
        if self.padded:
            x, length = x  # input consists of data and prepadded seq lens

        x = self.encoder(x, integration_timesteps)
        if self.mode in ["pool"]:
            # Perform mean pooling across time
            if self.padded:
                x = masked_meanpool(x, length)
            else:
                x = np.mean(x, axis=0)

        elif self.mode in ["last"]:
            # Just take the last state
            if self.padded:
                raise NotImplementedError("Mode must be in ['pool'] for self.padded=True (for now...)")
            else:
                x = x[-1]
        else:
            raise NotImplementedError("Mode must be in ['pool', 'last]")

        x = self.decoder(x)
        return nn.log_softmax(x, axis=-1)


# Here we call vmap to parallelize across a batch of input sequences
BatchClassificationModel = nn.vmap(
    ClassificationModel,
    in_axes=(0, 0),
    out_axes=0,
    variable_axes={"params": None, "dropout": None, 'batch_stats': None, "cache": 0, "prime": None},
    split_rngs={"params": False, "dropout": True}, axis_name='batch')


class GaussianRegressionModel(nn.Module):
    """
    """
    ssm: nn.Module
    d_output: int
    d_model: int
    n_layers: int
    padded: bool
    activation: str = "gelu"
    dropout: float = 0.2
    training: bool = True
    mode: str = "pool"
    prenorm: bool = False
    batchnorm: bool = False
    bn_momentum: float = 0.9
    step_rescale: float = 1.0

    append_integration_timestep: bool = False
    use_integration_timestep: bool = True
    encoder_fn: Callable = nn.Dense

    decoder_dim: int = 30  # CRU used a decoder dim of 30.

    def setup(self):
        """
        Initializes the S5 stacked encoder and a linear decoder.
        """
        self.encoder = StackedEncoderModel(
            ssm=self.ssm,
            d_model=self.d_model,
            n_layers=self.n_layers,
            activation=self.activation,
            dropout=self.dropout,
            training=self.training,
            prenorm=self.prenorm,
            batchnorm=self.batchnorm,
            bn_momentum=self.bn_momentum,
            step_rescale=self.step_rescale,
            append_integration_timestep=self.append_integration_timestep,
            use_integration_timestep=self.use_integration_timestep,
            encoder_fn=self.encoder_fn
        )

        self.value_head = SimpleMLP([self.decoder_dim, self.decoder_dim, 1])
        self.advantage_head = SimpleMLP([self.decoder_dim, self.decoder_dim, self.d_output])
        # Need two outputs (mean, log-var).
        # self.decoder_mu = SimpleMLP([self.decoder_dim, self.d_output])
        # self.decoder_lvar = SimpleMLP([self.decoder_dim, self.d_output])

    def __call__(self, x, training, integration_timesteps=None):
        """
        Args:
             x (float32): input sequence (L, d_input)
        Returns:
            output (float32): (d_output, d_output)  Mean and variance.
        """
        x = self.encoder(x, training, integration_timesteps)
        if self.mode in ["pool"]:
            x = np.mean(x, axis=0)
        elif self.mode in ["last"]:
            x = x[-1]
        v = self.value_head(x)
        a = self.advantage_head(x)
        # mu = self.decoder_mu(x)
        # var = self.decoder_lvar(x)

        return v, a
        # CRU uses a funny activtion function.
        # lvar_rectified = np.exp(lvar)
        # var = np.where(lvar < 0.0, lvar_rectified, lvar + 1.0)
        # return mu, var


# Here we call vmap to parallelize across a batch of input sequences
BatchGaussianRegressionModel = nn.vmap(
    GaussianRegressionModel,
    in_axes=(0, 0),
    out_axes=0,
    variable_axes={"params": None, "dropout": None, 'batch_stats': None, "cache": 0, "prime": None},
    split_rngs={"params": False, "dropout": True}, axis_name='batch'
)


# For Document matching task (e.g. AAN)
class RetrievalDecoder(nn.Module):
    """
    Defines the decoder to be used for document matching tasks,
    e.g. the AAN task. This is defined as in the S4 paper where we apply
    an MLP to a set of 4 features. The features are computed as described in
    Tay et al 2020 https://arxiv.org/pdf/2011.04006.pdf.
    Args:
        d_output    (int32):    the output dimension, i.e. the number of classes
        d_model     (int32):    this is the feature size of the layer inputs and outputs
                    we usually refer to this size as H
    """
    d_model: int
    d_output: int

    def setup(self):
        """
        Initializes 2 dense layers to be used for the MLP.
        """
        self.layer1 = nn.Dense(self.d_model)
        self.layer2 = nn.Dense(self.d_output)

    def __call__(self, x):
        """
        Computes the input to be used for the softmax function given a set of
        4 features. Note this function operates directly on the batch size.
        Args:
             x (float32): features (bsz, 4*d_model)
        Returns:
            output (float32): (bsz, d_output)
        """
        x = self.layer1(x)
        x = nn.gelu(x)
        return self.layer2(x)


class RetrievalModel(nn.Module):
    """ S5 Retrieval classification model. This consists of the stacked encoder
    (which consists of a linear encoder and stack of S5 layers), mean pooling
    across the sequence length, constructing 4 features which are fed into a MLP,
    and a softmax operation. Note that unlike the standard classification model above,
    the apply function of this model operates directly on the batch of data (instead of calling
    vmap on this model).
        Args:
            ssm         (nn.Module): the SSM to be used (i.e. S5 ssm)
            d_output     (int32):    the output dimension, i.e. the number of classes
            d_model     (int32):    this is the feature size of the layer inputs and outputs
                        we usually refer to this size as H
            n_layers    (int32):    the number of S5 layers to stack
            padded:     (bool):     if true: padding was used
            activation  (string):   Type of activation function to use
            dropout     (float32):  dropout rate
            training    (bool):     whether in training mode or not
            prenorm     (bool):     apply prenorm if true or postnorm if false
            batchnorm   (bool):     apply batchnorm if true or layernorm if false
            bn_momentum (float32):  the batchnorm momentum if batchnorm is used
    """
    ssm: nn.Module
    d_output: int
    d_model: int
    n_layers: int
    padded: bool
    activation: str = "gelu"
    dropout: float = 0.2
    training: bool = True
    prenorm: bool = False
    batchnorm: bool = False
    bn_momentum: float = 0.9
    step_rescale: float = 1.0

    def setup(self):
        """
        Initializes the S5 stacked encoder and the retrieval decoder. Note that here we
        vmap over the stacked encoder model to work well with the retrieval decoder that
        operates directly on the batch.
        """
        BatchEncoderModel = nn.vmap(
            StackedEncoderModel,
            in_axes=(0, 0),
            out_axes=0,
            variable_axes={"params": None, "dropout": None, 'batch_stats': None, "cache": 0, "prime": None},
            split_rngs={"params": False, "dropout": True}, axis_name='batch'
        )

        self.encoder = BatchEncoderModel(
                            ssm=self.ssm,
                            d_model=self.d_model,
                            n_layers=self.n_layers,
                            activation=self.activation,
                            dropout=self.dropout,
                            training=self.training,
                            prenorm=self.prenorm,
                            batchnorm=self.batchnorm,
                            bn_momentum=self.bn_momentum,
                            step_rescale=self.step_rescale,
                                        )
        BatchRetrievalDecoder = nn.vmap(
            RetrievalDecoder,
            in_axes=0,
            out_axes=0,
            variable_axes={"params": None},
            split_rngs={"params": False},
        )

        self.decoder = BatchRetrievalDecoder(
                                d_model=self.d_model,
                                d_output=self.d_output
                                          )

    def __call__(self, input, integration_timesteps):  # input is a tuple of x and lengths
        """
        Compute the size d_output log softmax output given a
        Lxd_input input sequence. The encoded features are constructed as in
        Tay et al 2020 https://arxiv.org/pdf/2011.04006.pdf.
        Args:
             input (float32, int32): tuple of input sequence and prepadded sequence lengths
                input sequence is of shape (2*bsz, L, d_input) (includes both documents) and
                lengths is (2*bsz,)
        Returns:
            output (float32): (d_output)
        """
        x, lengths = input  # x is 2*bsz*seq_len*in_dim, lengths is: (2*bsz,)
        x = self.encoder(x, integration_timesteps)  # The output is: 2*bszxseq_lenxd_model
        outs = batch_masked_meanpool(x, lengths)  # Avg non-padded values: 2*bszxd_model
        outs0, outs1 = np.split(outs, 2)  # each encoded_i is bszxd_model
        features = np.concatenate([outs0, outs1, outs0-outs1, outs0*outs1], axis=-1)  # bszx4*d_model
        out = self.decoder(features)
        return nn.log_softmax(out, axis=-1)
