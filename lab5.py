from __future__ import unicode_literals, print_function, division
from io import open
import unicodedata
import string
import re
import random
import time
import math
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import matplotlib.pyplot as plt
plt.switch_backend('agg')
import matplotlib.ticker as ticker
import numpy as np
from os import system
from nltk.translate.bleu_score import SmoothingFunction, sentence_bleu

import dataloader as dl



"""==============================================================================
The sample.py includes the following template functions:

1. Encoder, decoder
2. Training function
3. BLEU-4 score function

You have to modify them to complete the lab.
In addition, there are still other functions that you have to 
implement by yourself.

1. The reparameterization trick
2. Your own dataloader (design in your own way, not necessary Pytorch Dataloader)
3. Output your results (BLEU-4 score, words)
4. Plot loss/score
5. Load/save weights

There are some useful tips listed in the lab assignment.
You should check them before starting your lab.
================================================================================"""

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

MAX_LENGTH = 15

SOS_token = 0
EOS_token = 1
#----------Hyper Parameters----------#
hidden_size = 256
#The number of vocabulary
vocab_size = 28
Teacher_forcing_ratio = 0.7
empty_input_ratio = 0.1
KLD_weight = 0.0
LR = 0.01
LAYER_DEPTH = 1

latent_size = 32
cond_size = 8

epochs = 20

print_every = 10
save_every = 1000

anneal_function = 'linear'
x0 = 1000
k = 0.0025
inv = 1000

#The target word
reference = 'accessed'
#The word generated by your model
output = 'access'

# One-Hot
def kl_anneal_function(anneal_function, step, k, x0):
    step = step % inv
    if anneal_function == 'logistic':
        return float(1 / (1 + np.exp(-k * (step - x0))))
    elif anneal_function == 'linear':
        return min(1, step / x0)

#compute BLEU-4 score
def compute_bleu(output, reference):
    cc = SmoothingFunction()
    return sentence_bleu([reference], output,weights=(0.25, 0.25, 0.25, 0.25),smoothing_function=cc.method1)

#Encoder
class EncoderBiLSTM(nn.Module):
    """Vanilla encoder using pure LSTM."""
    def __init__(self, input_size, hidden_size, n_layers=LAYER_DEPTH):
        super(EncoderBiLSTM, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size
        self.embedding = nn.Embedding(input_size, hidden_size)
        self.bilstm = nn.LSTM(hidden_size, hidden_size // 2, num_layers=n_layers, bidirectional=True, batch_first=False)

        self.hidden2mean = nn.Linear(hidden_size, latent_size)
        self.hidden2logv = nn.Linear(hidden_size, latent_size)

        self.tense_embedding = nn.Embedding(4, cond_size)
        self.cond2hidden = nn.Linear(latent_size+cond_size, hidden_size)

    def embed(self, X):
        return self.embedding(X).view(-1, 1, 256)

    def forward(self, input, hidden, condition=0):
        # embedded is of size (n_batch, seq_len, emb_dim)
        # lstm needs: (seq_len, batch, input_size)
        # lstm output: (seq_len, batch, hidden_size * num_directions)
        seq_len = input.size(0)

        embedded = self.embed(input)

        cond = self.tense_embedding(torch.cuda.LongTensor([condition])).view(-1, 1, cond_size)

        h0, c0 = hidden

        h0 = torch.cat((h0, cond), 2)

        h0 = self.cond2hidden(h0).view(2,1,-1)

        outputs, hidden = self.bilstm(embedded, (h0, c0))
        hn, cn = hidden
        hn = hn.view(1,-1)

        mean = self.hidden2mean(hn)
        logv = self.hidden2logv(hn)
        std = torch.exp(0.5 * logv)

        return mean,logv,std,outputs, hidden

    def initHidden(self):
        forward = Variable(torch.zeros(1, 1, latent_size), requires_grad=False).cuda()
        backward = Variable(torch.zeros(2 * self.n_layers, 1, self.hidden_size // 2), requires_grad=False).cuda()

        return (forward, backward)


#Decoder
class DecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size):
        super(DecoderRNN, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.lstm = nn.LSTM(hidden_size, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

        self.tense_embedding = nn.Embedding(4, cond_size)
        self.latent2hidden = nn.Linear(latent_size +cond_size, hidden_size)



    def forward(self, input, hidden, encoder_outputs, first = False, z = 0, condition = 0):
        if first:
            cond = self.tense_embedding(torch.cuda.LongTensor([condition])).view(-1, 1, cond_size)
            z = z.unsqueeze(0)
            z = torch.cat((z, cond), 2)
            h0 = self.latent2hidden(z)
            c0 = torch.zeros(1, 1, self.hidden_size, device=device)
            hidden = (h0, c0)

        output = self.embedding(input).view(-1, 1, self.hidden_size)
        output = F.relu(output)


        output, hidden = self.lstm(output, hidden)
        output = self.out(output[0])
        attn = None
        return output, hidden, attn


    def initHidden(self):
        return torch.zeros(1, 1, self.hidden_size, device=device)

class Attn(nn.Module):
    """ The score function for the attention mechanism.
    We define the score function as the general function from Luong et al.
    Where score(s_{i}, h_{j}) = s_{i} * W * h_{j}
    """
    def __init__(self, hidden_size):
        super(Attn, self).__init__()
        self.hidden_size = hidden_size
        self.attn = nn.Linear(self.hidden_size, self.hidden_size)

    def forward(self, hidden, encoder_outputs):
        batch_size, seq_len, hidden_size = encoder_outputs.size()
        # print(encoder_outputs.size())

        # Get hidden chuncks (batch_size, seq_len, hidden_size)
        hidden = hidden.unsqueeze(1)  # (batch_size, 1, hidden_size)
        hiddens = hidden.repeat(1, seq_len, 1)
        attn_energies = self.score(hiddens, encoder_outputs)

        # # Calculate energies for each encoder output
        # for i in range(seq_len):
        #     attn_energies[:, i] = self.score(hidden, encoder_outputs[:, i])
        # print(attn_energies.size())

        # Normalize energies to weights in range 0 to 1, resize to B x 1 x seq_len
        return F.softmax(attn_energies, dim=1).unsqueeze(1)

    def score(self, hidden, encoder_outputs):
        # print('size of hidden: {}'.format(hidden.size()))
        # print('size of encoder_hidden: {}'.format(encoder_output.size()))
        energy = self.attn(encoder_outputs)

        # batch-wise calculate dot-product
        hidden = hidden.unsqueeze(2)  # (batch, seq, 1, d)
        energy = energy.unsqueeze(3)  # (batch, seq, d, 1)

        energy = torch.matmul(hidden, energy)  # (batch, seq, 1, 1)

        # print('size of energies: {}'.format(energy.size()))

        return energy.squeeze(3).squeeze(2)

class AttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size):
        super(AttnDecoderRNN, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

        self.attn = Attn(hidden_size)


    def forward(self, input, hidden, encoder_outputs):
        output = self.embedding(input).view(-1, 1, 256)
        output = F.relu(output)

        attn_weights = self.attn(hidden[-1, :, :], encoder_outputs)
        context = torch.bmm(attn_weights, encoder_outputs)

        # Adjust the dimension after bmm()
        context = context.squeeze(1)
        print(context.shape)
        print(output.shape)
        output = torch.cat((output.squeeze(0), context), dim=0)

        # To align with the library standard (seq_len, batch, input_size)
        output = output.unsqueeze(0)
        print(output.shape)


        output, hidden = self.gru(output, hidden)
        output = self.out(output[0])
        attn = None
        return output, hidden, attn


    def initHidden(self):
        return torch.zeros(1, 1, self.hidden_size, device=device)


def train(input_tensor, target_tensor, tense_cond, encoder, decoder, encoder_optimizer, decoder_optimizer,step, criterion, max_length=MAX_LENGTH,teacher_forcing_ratio=0.7):


    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()


    input_length = input_tensor.size(0)
    target_length = target_tensor.size(0)


    encoder_outputs = torch.zeros(max_length, encoder.hidden_size, device=device)

    loss = 0

    #----------sequence to sequence part for encoder----------#
    encoder_hidden = encoder.initHidden()
    mean, logv, std, encoder_outputs, encoder_hidden = encoder(input_tensor, encoder_hidden, tense_cond)

    z = Variable(torch.randn([1, latent_size]).to(device))
    z = z * std + mean

    decoder_input = torch.tensor([[SOS_token]], device=device)

    decoder_hidden = encoder_hidden
    #decoder_hidden[0,:,:] = encoder_outputs[-1,:,:]

    use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False
	

    #----------sequence to sequence part for decoder----------#
    KL_loss = -0.5 * torch.sum(1 + logv - mean.pow(2) - logv.exp())
    KL_weight = kl_anneal_function(anneal_function, step, k, x0)
    KL_term = KL_loss * KL_weight

    if use_teacher_forcing:
        # Teacher forcing: Feed the target as the next input
        first = True
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs, first, z, tense_cond)
            loss += criterion(decoder_output, target_tensor[di].reshape(1))

            decoder_input = target_tensor[di]  # Teacher forcing
            first = False

    else:
        # Without teacher forcing: use its own predictions as the next input
        first = True
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs, first, z, tense_cond)
            first = False
            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze().detach()  # detach from history as input
            #print('====')
            #print(decoder_output.shape)
            loss += criterion(decoder_output, target_tensor[di].reshape(1))
            if decoder_input.item() == EOS_token:
                break

    loss += KL_term * 0.1
    loss.backward()

    encoder_optimizer.step()
    decoder_optimizer.step()

    return loss.item() / target_length, KL_weight, KL_loss

def getOutputFromText(input, encoder, decoder, condFrom, condTo):
    lis = dl.stringToList(input)
    output_len = len(input)
    input_tensor = Variable(toTensor(lis))

    encoder_hidden = encoder.initHidden()
    mean,logv,std,encoder_outputs, encoder_hidden = encoder(input_tensor, encoder_hidden, condFrom)

    z = Variable(torch.randn([1, latent_size]).to(device))
    z = z * std + mean

    decoder_input = torch.tensor([[SOS_token]], device=device)

    decoder_hidden = encoder_hidden

    res = ''
    first = True
    for i in range(output_len*2):
        decoder_output, decoder_hidden, decoder_attention = decoder(
            decoder_input, decoder_hidden, encoder_outputs, first, z, condTo)

        topv, topi = decoder_output.topk(1)
        decoder_input = topi.squeeze().detach()  # detach from history as input

        first = False

        if decoder_input.item() != SOS_token and decoder_input.item() != EOS_token:
            res += chr(decoder_input.item()+95)


        if decoder_input.item() == EOS_token:
            break

    print(res)

def genTensesByGaussian(decoder):
    z = Variable(torch.randn([1, latent_size]).to(device))
    std = torch.randn([1, latent_size]).to(device)
    mean = torch.randn([1, latent_size]).to(device)
    z = z * std + mean

    for cond in range(4):

        decoder_input = torch.tensor([[SOS_token]], device=device)

        res = ''
        first = True
        decoder_hidden = None
        while True:
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, None, first, z, cond)

            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze().detach()  # detach from history as input

            first = False

            if decoder_input.item() != SOS_token and decoder_input.item() != EOS_token:
                res += chr(decoder_input.item() + 95)

            if decoder_input.item() == EOS_token:
                break

        print(res)

def calcTestBLEU(testdt, encoder, decoder):
    TotalBLEU = 0
    for i in range(testdt.N):
        input = testdt.X[i][0]
        target = testdt.X[i][1]
        condFrom = testdt.Tense[i][0]
        condTo = testdt.Tense[i][1]

        lis = dl.stringToList(input)
        output_len = len(input)
        input_tensor = Variable(toTensor(lis))

        encoder_hidden = encoder.initHidden()
        mean,logv,std,encoder_outputs, encoder_hidden = encoder(input_tensor, encoder_hidden, condFrom)

        z = Variable(torch.randn([1, latent_size]).to(device))
        z = z * std + mean

        decoder_input = torch.tensor([[SOS_token]], device=device)

        decoder_hidden = encoder_hidden

        res = ''
        first = True
        for i in range(output_len*2):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs, first, z, condTo)

            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze().detach()  # detach from history as input

            first = False

            if decoder_input.item() != SOS_token and decoder_input.item() != EOS_token:
                res += chr(decoder_input.item()+95)


            if decoder_input.item() == EOS_token:
                break

        TotalBLEU += compute_bleu(target, res)
    return TotalBLEU / testdt.N

def calcTestBLEUAndShow(testdt, encoder, decoder):
    TotalBLEU = 0
    for i in range(testdt.N):
        input = testdt.X[i][0]
        target = testdt.X[i][1]
        condFrom = testdt.Tense[i][0]
        condTo = testdt.Tense[i][1]

        lis = dl.stringToList(input)
        output_len = len(input)
        input_tensor = Variable(toTensor(lis))

        encoder_hidden = encoder.initHidden()
        mean,logv,std,encoder_outputs, encoder_hidden = encoder(input_tensor, encoder_hidden, condFrom)

        z = Variable(torch.randn([1, latent_size]).to(device))
        z = z * std + mean

        decoder_input = torch.tensor([[SOS_token]], device=device)

        decoder_hidden = encoder_hidden

        res = ''
        first = True
        for i in range(output_len*2):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs, first, z, condTo)

            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze().detach()  # detach from history as input

            first = False

            if decoder_input.item() != SOS_token and decoder_input.item() != EOS_token:
                res += chr(decoder_input.item()+95)


            if decoder_input.item() == EOS_token:
                break

        print('%s(%s)->(%s):'%(input,dl.getTenseLabel(condFrom),dl.getTenseLabel(condTo)))
        print('predict:%s'%res)
        print('truth:%s'%target)

        B = compute_bleu(target, res)

        print('BLEU:%f'% B)

        TotalBLEU += B
    print('Total AVG BLEU=%f' % (TotalBLEU / testdt.N))

def asMinutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return '%dm %ds' % (m, s)


def timeSince(since, percent):
    now = time.time()
    s = now - since
    es = s / (percent)
    rs = es - s
    return '%s (- %s)' % (asMinutes(s), asMinutes(rs))



def trainIters(dataloader, encoder, decoder, epochs, print_every=1000, plot_every=100, learning_rate=0.01, teacher_forcing_ratio=0.7):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    encoder_optimizer = optim.SGD(encoder.parameters(), lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)

    n_iters = dataloader.PairN * epochs

    criterion = nn.CrossEntropyLoss()

    # recorder
    KLLossRecorder = np.zeros(n_iters)
    LossRecorder = np.zeros(n_iters)
    BLEURecorder = np.zeros(n_iters)

    for epoch in range(epochs):
        epoch_iter = dataloader.PairN * epoch
        training_pairs = dataloader.genShufflePairs()


        for iter in range(1, dataloader.PairN + 1):
            encoder1.train()
            decoder1.train()

            global_iter = epoch_iter + iter - 1
            #print('Iter %d' % iter)
            training_pair = training_pairs[iter-1]
            input_tensor = Variable(toTensor(training_pair[0]))
            target_tensor = toTensor(training_pair[1])
            tense_cond = training_pair[2]



            loss,KL_weight,KL_loss = train(input_tensor, target_tensor, tense_cond, encoder,
                     decoder, encoder_optimizer, decoder_optimizer,step=iter,criterion=criterion,teacher_forcing_ratio=teacher_forcing_ratio)
            print_loss_total += loss
            plot_loss_total += loss



            LossRecorder[global_iter] = loss
            KLLossRecorder[global_iter] = KL_loss


            encoder1.eval()
            decoder1.eval()


            BLEURecorder[global_iter] = B = calcTestBLEU(test_pairs, encoder1, decoder1)

            if global_iter % 25000 == 0:
                teacher_forcing_ratio -= 0.1

            if global_iter % 5000 == 0:
                learning_rate *= 0.87
                encoder_optimizer = optim.SGD(encoder.parameters(), lr=learning_rate)
                decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)

            if global_iter % save_every == 0:
                np.savetxt('BLEU.txt', BLEURecorder[0:global_iter+1])
                np.savetxt('Loss.txt', LossRecorder[0:global_iter+1])
                np.savetxt('KLLoss.txt', KLLossRecorder[0:global_iter+1])

            if iter % print_every == 0:
                print_loss_avg = print_loss_total / print_every
                print_loss_total = 0
                print('%s (%d-%d %d%%) %.4f' % (timeSince(start, (global_iter) / n_iters), epoch,
                                             (global_iter), (global_iter) / n_iters * 100, print_loss_avg)
                                            , end='|')
                print("KL Loss = %f" % (KL_loss.item()), end='|')
                print("KL Weight = %f" % KL_weight, end='|')
                print("BLEU = %f" % B)

            if B > 0.7:
                torch.save(encoder1, 'e1_BLEU.pkl')
                torch.save(decoder1, 'e1_BLEU.pkl')


        torch.save(encoder1, 'e1_epoch%d.pkl' % epoch)
        torch.save(decoder1, 'd1_epoch%d.pkl' % epoch)




def toTensor(a):
    return  torch.LongTensor(a).to(device)




encoder1 = EncoderBiLSTM(vocab_size, hidden_size).to(device)
decoder1 = DecoderRNN(hidden_size, vocab_size).to(device)

encoder1.train()
decoder1.train()

pairs = dl.dataloader('train')
test_pairs = dl.dataloader('test')



Train = 1
if Train == 1:
    trainIters(pairs, encoder1, decoder1, epochs, print_every=5,learning_rate=LR,teacher_forcing_ratio=Teacher_forcing_ratio)

else:
    encoder1 = torch.load('e1_epoch19.pkl')
    decoder1 = torch.load('d1_epoch19.pkl')


encoder1.eval()
decoder1.eval()


#calcTestBLEUAndShow(test_pairs, encoder1, decoder1)
for h in range(100):
    print(h)
    genTensesByGaussian(decoder1)

exit()

while True:
    cond = list(map(int,input('(tense from,tense to):').split(',')))
    str = input('String:')
    if str != '0':
        getOutputFromText(str,encoder1, decoder1,cond[0],cond[1])
    else:
        exit()
