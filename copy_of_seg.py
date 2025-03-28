# -*- coding: utf-8 -*-
"""Copy of seg.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/12aqHnJdNCffuYMaprPgPjajJRWyuKgDV
"""

!pip install segmentation_models_pytorch
!pip install torchmetrics

from google.colab import drive
drive.mount("/gdrive")

import torch
from torch import nn
from torch.nn import functional as F
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader
from torchsummary import summary

import numpy as np
import matplotlib.pyplot as plt
import os
import timm
import cv2
from tqdm.notebook import tqdm
from sklearn.metrics import jaccard_score as jsc
import segmentation_models_pytorch as smp
from sklearn.model_selection import train_test_split

#hyperaparameters

BATCH_SIZE = 16
N_EPOCHS = 150
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
TRAIN_ROOT_DIR = "/gdrive/MyDrive/eye/eye"
OCT_ROOT_DIR = TRAIN_ROOT_DIR +"/oct"
IRF_ROOT_DIR = TRAIN_ROOT_DIR +"/irf"
SRF_ROOT_DIR = TRAIN_ROOT_DIR +"/srf"

class DoubleConv(nn.Module):
    """(convolution => [BN] => ReLU) * 2"""

    def __init__(self, in_channels, out_channels, mid_channels=None):
        super().__init__()
        if not mid_channels:
            mid_channels = out_channels
        self.double_conv = nn.Sequential(
            nn.Conv2d(in_channels, mid_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(mid_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x):
        return self.double_conv(x)


class Down(nn.Module):
    """Downscaling with maxpool then double conv"""

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.maxpool_conv = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(in_channels, out_channels)
        )

    def forward(self, x):
        return self.maxpool_conv(x)


class Up(nn.Module):
    """Upscaling then double conv"""

    def __init__(self, in_channels, out_channels, bilinear=True):
        super().__init__()

        # if bilinear, use the normal convolutions to reduce the number of channels
        if bilinear:
            self.up = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
            self.conv = DoubleConv(in_channels, out_channels, in_channels // 2)
        else:
            self.up = nn.ConvTranspose2d(in_channels, in_channels // 2, kernel_size=2, stride=2)
            self.conv = DoubleConv(in_channels, out_channels)

    def forward(self, x1, x2):
        x1 = self.up(x1)
        # input is CHW
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]

        x1 = F.pad(x1, [diffX // 2, diffX - diffX // 2,
                        diffY // 2, diffY - diffY // 2])
        # if you have padding issues, see
        # https://github.com/HaiyongJiang/U-Net-Pytorch-Unstructured-Buggy/commit/0e854509c2cea854e247a9c615f175f76fbb2e3a
        # https://github.com/xiaopeng-liao/Pytorch-UNet/commit/8ebac70e633bac59fc22bb5195e513d5832fb3bd
        x = torch.cat([x2, x1], dim=1)
        return self.conv(x)


class OutConv(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(OutConv, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

    def forward(self, x):
        return self.conv(x)
class UNet(nn.Module):
    def __init__(self, n_channels, n_classes, bilinear=False):
        super(UNet, self).__init__()
        self.n_channels = n_channels
        self.n_classes = n_classes
        self.bilinear = bilinear

        self.inc = DoubleConv(n_channels, 64)
        self.down1 = Down(64, 128)
        self.down2 = Down(128, 256)
        self.down3 = Down(256, 512)
        factor = 2 if bilinear else 1
        self.down4 = Down(512, 1024 // factor)

        self.Fup1 = Up(1024, 512 // factor, bilinear)
        self.Fup2 = Up(512, 256 // factor, bilinear)
        self.Fup3 = Up(256, 128 // factor, bilinear)
        self.Fup4 = Up(128, 64, bilinear)
        self.Foutc = OutConv(64, n_classes)

        self.Sup1 = Up(1024, 512 // factor, bilinear)
        self.Sup2 = Up(512, 256 // factor, bilinear)
        self.Sup3 = Up(256, 128 // factor, bilinear)
        self.Sup4 = Up(128, 64, bilinear)
        self.Soutc = OutConv(64, n_classes)

    def forward(self, x):
        x1 = self.inc(x)
        x2 = self.down1(x1)
        x3 = self.down2(x2)
        x4 = self.down3(x3)
        x5 = self.down4(x4)

        Fx = self.Fup1(x5, x4)
        Fx = self.Fup2(Fx, x3)
        Fx = self.Fup3(Fx, x2)
        Fx = self.Fup4(Fx, x1)
        Flogits = self.Foutc(Fx)

        Sx = self.Sup1(x5, x4)
        Sx = self.Sup2(Sx, x3)
        Sx = self.Sup3(Sx, x2)
        Sx = self.Sup4(Sx, x1)
        Slogits = self.Soutc(Sx)

        return Flogits, Slogits

model = UNet(1, 1)

dummy = torch.zeros((1,1, 224,224))
dummy_op = model(dummy)
print(dummy_op[0].size())

oct_names = os.listdir(OCT_ROOT_DIR)
irf_names = os.listdir(IRF_ROOT_DIR)
srf_names = os.listdir(SRF_ROOT_DIR)

print(len(srf_names))
print(len(irf_names))
print(len(oct_names))

class OCT_data(Dataset):
    def __init__(self, filenames, root_dirs, device=DEVICE, test=False):
        self.filenames = filenames
        self.device = device
        self.oct_rt_dir = root_dirs[0]
        self.test = test
        if not self.test:
            self.irf_rt_dir = root_dirs[1]
            self.srf_rt_dir = root_dirs[2]

    def __len__(self,):
        return len(self.filenames)

    def read_img(self, img_name, rt_dir):
        img = cv2.imread(os.path.join(rt_dir, img_name), cv2.IMREAD_GRAYSCALE)
        img = cv2.resize(img, (224, 224)).reshape((-1, 224, 224))
        return img/255.0

    def __getitem__(self, id):
        self.oct = self.read_img(self.filenames[id], self.oct_rt_dir)

        if not self.test:
            self.irf = self.read_img(self.filenames[id], self.irf_rt_dir)
            self.srf = self.read_img(self.filenames[id], self.srf_rt_dir)

            self.srf[self.srf>=0.5] = 1
            self.srf[self.srf<0.5] = 0
            self.irf[self.irf>=0.5] = 1
            self.irf[self.irf<0.5] = 0


            self.oct = torch.tensor(self.oct, dtype = torch.float).to(self.device)
            self.irf = torch.tensor(self.irf, dtype = torch.float).to(self.device)
            self.srf = torch.tensor(self.srf, dtype = torch.float).to(self.device)

            return self.oct, self.irf, self.srf

        self.oct = torch.tensor(self.oct, dtype = torch.float).to(self.device)
        return self.oct

dummy = OCT_data(oct_names,
                [
                 OCT_ROOT_DIR, IRF_ROOT_DIR, SRF_ROOT_DIR
                ], device = "cpu")

d_oct, d_irf, d_srf = dummy.__getitem__(11)
print(d_oct.size())
plt.imshow(d_oct.permute(1,2,0)[:,:,0])
plt.show()

plt.imshow(d_irf.permute(1,2,0)[:,:,0])
plt.show()
plt.imshow(d_srf.permute(1,2,0)[:,:,0])
plt.show()


print(np.unique(d_irf), np.unique(d_srf))

train_filenames, val_filenames = train_test_split(oct_names, test_size=0.1)
train_dataset = OCT_data(train_filenames,
                [
                 OCT_ROOT_DIR,
                 IRF_ROOT_DIR,
                 SRF_ROOT_DIR
                ], device = DEVICE)

val_dataset = OCT_data(val_filenames,
                [
                 OCT_ROOT_DIR,
                 IRF_ROOT_DIR,
                 SRF_ROOT_DIR
                ], device = DEVICE
                )

train_dataloader = DataLoader(
    train_dataset,
    batch_size = 16,
    shuffle = True
)

val_dataloader = DataLoader(
    val_dataset,
    batch_size = 16,
    shuffle = True
)

loss_fn = smp.losses.DiceLoss(smp.losses.BINARY_MODE, from_logits=True)
optimizer = Adam(lr = 0.0005, params = model.parameters())

model = model.to(DEVICE)

losses = {
    "train":[float("inf")],
    "val":[float("inf")]
}

for epoch in range(N_EPOCHS):
    L = 0
    for i,(oct_batch, irf_batch, srf_batch) in enumerate(tqdm(train_dataloader)):
        irf_batch_pred, srf_batch_pred = model(oct_batch)
        l = loss_fn(irf_batch_pred, irf_batch) + loss_fn(srf_batch_pred, srf_batch)
        optimizer.zero_grad()
        l.backward()
        optimizer.step()
        L+=l.item()

    if(L<min(losses["train"])):
        torch.save(model.state_dict(), "./model.pt")
        print("#"*20, f"model saved at {epoch} epoch and {L} loss...")
    print(f"epoch : {epoch} | loss:{L}")
    losses["train"].append(L)

plt.plot(range(len(losses["train"][1:])) ,losses["train"][1:])
plt.show()

model.load_state_dict(torch.load("/gdrive/MyDrive/eye/eye/model.pt"))
model.eval()

irf_outputs = []
srf_outputs = []
irf_GTs = []
srf_GTs = []
for i,(oct_batch, irf_batch, srf_batch) in enumerate(tqdm(val_dataloader)):
    irf_batch_pred, srf_batch_pred = model(oct_batch)
    irf_prob_mask = irf_batch_pred.sigmoid()
    irf_pred_mask = (irf_prob_mask > 0.5).float()

    srf_prob_mask = srf_batch_pred.sigmoid()
    srf_pred_mask = (srf_prob_mask > 0.5).float()

    srf_pred_mask = srf_pred_mask.cpu().detach().numpy()
    irf_pred_mask = irf_pred_mask.cpu().detach().numpy()


    srf_batch = srf_batch.cpu().detach().numpy()
    irf_batch = irf_batch.cpu().detach().numpy()

    irf_outputs+=list(irf_pred_mask.reshape(-1))
    srf_outputs+=list(srf_pred_mask.reshape(-1))
    irf_GTs+=list(irf_batch.reshape(-1))
    srf_GTs+=list(srf_batch.reshape(-1))
    print(jsc(srf_batch.reshape(-1), srf_pred_mask.reshape(-1)))

for i in range(srf_batch.shape[0]):
    fig, ax = plt.subplots(2,2, figsize = (10,10))
    ax[0][0].imshow(srf_batch[i][0])
    ax[0][0].set_title("srf GT")
    ax[0][1].imshow(srf_pred_mask[i][0])
    ax[0][1].set_title("srf pred")
    ax[1][0].imshow(irf_batch[i][0])
    ax[1][0].set_title("irf GT")
    ax[1][1].imshow(irf_pred_mask[i][0])
    ax[1][1].set_title("irf pred")
    plt.show()

"""## Inference

"""

TEST_OCT_DIR = "/gdrive/MyDrive/eye/test_oct"
TEST_RESULTS_DIR = "/gdrive/MyDrive/eye/test_results"

test_imgs = os.listdir(TEST_OCT_DIR)
test_img_dirs = [TEST_OCT_DIR+"/"+f for f in test_imgs]

test_dataset = OCT_data(test_img_dirs,
                [
                 TEST_OCT_DIR
                ], test = True, device = DEVICE
                )


test_dataloader = DataLoader(
    test_dataset,
    batch_size = 16,
    shuffle = True
)

irf_outputs = []
srf_outputs = []
for i,oct_batch in enumerate(tqdm(test_dataloader)):
    irf_batch_pred, srf_batch_pred = model(oct_batch)
    irf_prob_mask = irf_batch_pred.sigmoid()
    irf_pred_mask = (irf_prob_mask > 0.5).float()

    srf_prob_mask = srf_batch_pred.sigmoid()
    srf_pred_mask = (srf_prob_mask > 0.5).float()

    srf_pred_mask = srf_pred_mask.cpu().detach().numpy()
    irf_pred_mask = irf_pred_mask.cpu().detach().numpy()

oct_batch = oct_batch.cpu().detach().numpy()

for i in range(oct_batch.shape[0]):
    fig, ax = plt.subplots(1,3, figsize = (10,10))
    ax[0].imshow(oct_batch[i][0])
    ax[0].set_title("Input OCT")
    ax[1].imshow(srf_pred_mask[i][0])
    ax[1].set_title("srf pred")
    ax[2].imshow(irf_pred_mask[i][0])
    ax[2].set_title("irf pred")
    plt.savefig(TEST_RESULTS_DIR+"/"+str(i)+".png")
    plt.show()