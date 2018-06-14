import torch
from torch.distributions.multivariate_normal import MultivariateNormal
import torch.nn as nn
from torch.nn.functional import cross_entropy, softmax


# TODO: Collect stop indices and zero out those columns.
class TopicRNN(nn.Module):

    def __init__(self, vocab_size, embedding_size, hidden_size, batch_size, stop_indices,
                 device, vae_hidden_size=1024, layers=2, dropout=0.5, topic_dim=15,
                 train_embeddings=False, embedding_matrix=None, use_topics=True):

        """
        RNN Language model: Choose between Elman, LSTM, and GRU
        RNN architectures.

        Expects single

        Parameters:
        -----------
        :param embedding_size: int
            The embedding size for embedding input words (space in which
            words are projected).

        :param hidden_size: int
            The hidden size of the RNN
        """
        # Save the construction arguments, useful for serialization
        self.init_arguments = locals()
        self.init_arguments.pop("self")
        self.init_arguments.pop("__class__")
        super(TopicRNN, self).__init__()

        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.embedding_size = embedding_size
        self.batch_size = batch_size
        self.device = device
        self.layers = layers
        self.use_topics = use_topics

        """ TopicRNN-specific """
        self.topic_dim = topic_dim

        # Topic proportions randomly initialized (uniform dist).
        topic_proportions = torch.rand(topic_dim)
        topic_proportions /= torch.sum(topic_proportions)
        self.theta = topic_proportions

        self.stop_indices = torch.Tensor(stop_indices).long()

        # Topic distributions over words
        self.beta = nn.Parameter(torch.rand(topic_dim, vocab_size))

        # Parameters for the VAE that approximates a normal dist.
        self.g = G(vocab_size - len(stop_indices), vae_hidden_size, topic_dim).to(device)

        # mu
        self.w1 = nn.Parameter(torch.rand(vae_hidden_size))
        self.a1 = nn.Parameter(torch.rand(topic_dim))

        # sigma
        self.w2 = nn.Parameter(torch.rand(vae_hidden_size))
        self.a2 = nn.Parameter(torch.rand(topic_dim))

        # noise
        self.noise = MultivariateNormal(torch.zeros(topic_dim), torch.eye(topic_dim))

        """ General RNN parameters"""

        self.softmax = nn.Softmax()

        # Learned word embeddings (vocab_size x embedding_size)
        if embedding_matrix is not None:
            self.embedding = nn.Embedding.from_pretrained(embedding_matrix,
                                                          freeze=not train_embeddings)
        else:
            self.embedding = nn.Embedding(vocab_size, embedding_size,
                                          padding_idx=0)

        # Elman RNN, accepts vectors of length 'embedding_size'.
        self.rnn = nn.RNN(embedding_size, hidden_size, layers,
                          dropout=dropout,
                          batch_first=True)

        # Decode from hidden state space to vocab space.
        self.decoder = nn.Linear(hidden_size, vocab_size)

    def init_hidden(self, single_example=False):
        """
        Produce a new, initialized hidden state variable where all values
        are zero.
        :return: A torch Tensor.
        """

        weight = next(self.parameters()).data
        if single_example:
            return weight.new(self.layers, 1,
                              self.hidden_size).zero_()
        else:
            return weight.new(self.layers, self.batch_size,
                              self.hidden_size).zero_()

    def forward(self, input, hidden):
        # Embed the passage.
        # Shape: (batch, length (single word), embedding_size)
        embedded_passage = self.embedding(input)

        # Forward pass.
        # Shape (output): (1, hidden_size)
        # Shape (hidden): (layers, batch, hidden_size)
        output, hidden = self.rnn(embedded_passage, hidden)

        # Decode all intermediary hidden states.
        decoded = self.decoder(output)

        # Extract topics for each word
        # Shape: (batch, sequence, vocabulary)
        # Pairs of identical words will receive an equivalent topic addition.
        if self.use_topics:
            # View needed if batch size is 1.
            topic_additions = torch.mm(self.theta.view(self.batch_size, -1), self.beta)
            topic_additions.t()[self.stop_indices] = 0
            topic_additions = topic_additions.unsqueeze(1).expand_as(decoded)
            decoded += topic_additions

        return decoded, hidden

    def likelihood(self, input, hidden, term_frequencies, target):
        # 1. Compute Kullback-Leibler Divergence
        if not hidden:
            hidden = self.init_hidden()
            hidden = hidden.to(self.device)

        neg_kl_div = 0
        if term_frequencies is not None and self.use_topics:
            mapped_term_frequencies = self.g(term_frequencies)

            # Compute Gaussian parameters.
            mu = mapped_term_frequencies.matmul(self.w1) + self.a1
            log_sigma = mapped_term_frequencies.matmul(self.w2) + self.a2

            # A closed-form solution exists since we're assuming q
            # is drawn from a normal distribution.
            #
            # Sum along the topic dimension.
            neg_kl_div = 1 + 2 * log_sigma - (mu ** 2) - torch.exp(2 * log_sigma)
            neg_kl_div = torch.sum(neg_kl_div) / 2

            # Update topic proportions
            epsilon = self.noise.rsample().to(self.device)
            self.theta = softmax(mu + torch.exp(log_sigma) * epsilon, dim=-1).to(self.device)

        output, hidden = self.forward(input, hidden)
        log_probabilities = cross_entropy(output.view(output.size(0) * output.size(1), -1),
                                          target.contiguous().view(-1,))

        # Cross Entropy is already a negated negative likelihood but
        # the KL-Divergence isn't.
        return -neg_kl_div + log_probabilities, hidden


class G(nn.Module):
    """
    The feedforward network that projects term-frequencies into
    K-dimensional latent space.

    Used for calculating mu and sigma for the approximated normal.

    Parameters:
    -----------
    :param vc_dim: int
        The size of the vocabulary excluding stop words.

    :param hidden_size: int
        The hidden size of the inference network.

    :param topic_dim: int
        The latent space in which to project term-frequencies onto.
    """
    def __init__(self, vc_dim, hidden_size, topic_dim):
        self.init_arguments = locals()
        self.init_arguments.pop("self")
        self.init_arguments.pop("__class__")
        super(G, self).__init__()

        self.vc_dim = vc_dim
        self.hidden_size = hidden_size
        self.topic_dim = topic_dim
        self.model = nn.Sequential(
            nn.Linear(vc_dim, hidden_size * topic_dim),
            nn.ReLU(),
            nn.Linear(hidden_size * topic_dim, hidden_size * topic_dim),
            nn.ReLU()
        )

    def forward(self, term_frequencies):
        # Reshape to (K x E) space for calculation of mu and sigma.
        # Normalize along the topic dimension.
        output = self.model(term_frequencies)
        batch_size = term_frequencies.size(0)
        return nn.Softmax(dim=1)(output.view(batch_size, self.topic_dim,
                                             self.hidden_size))
