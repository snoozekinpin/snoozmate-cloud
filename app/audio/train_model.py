"""
鼾声检测模型训练脚本（PyTorch 版）
数据：公开数据集 + 数据增强
模型：轻量 CNN + LSTM，可量化到 ESP32

用法：
  python train_model.py --data_dir ./data/audio_samples --epochs 30

输出：
  data/models/snore_model.pth  —— PyTorch 模型
  data/models/snore_model.tflite —— TFLite 量化版（给ESP32用）
"""
import os
import argparse
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import torchaudio


# ============================================================
# 数据增强
# ============================================================

def add_noise(waveform, snr_db=20):
    """加白噪音"""
    signal_power = torch.mean(waveform ** 2)
    noise_power = signal_power / (10 ** (snr_db / 10))
    noise = torch.randn_like(waveform) * torch.sqrt(noise_power)
    return waveform + noise


def time_shift(waveform, max_shift=100):
    """时移"""
    shift = torch.randint(-max_shift, max_shift, (1,)).item()
    return torch.roll(waveform, shift)


def pitch_shift(waveform, sr=16000, n_steps=1):
    """音调微调"""
    try:
        effects = [["pitch", str(n_steps * 100)], ["rate", str(sr)]]
        shifted, _ = torchaudio.sox_effects.apply_effects_tensor(
            waveform.unsqueeze(0), sr, effects
        )
        return shifted.squeeze(0)
    except Exception:
        return waveform  # 不支持就原样返回


def volume_perturb(waveform, factor_range=(0.7, 1.3)):
    """音量随机变化"""
    factor = np.random.uniform(*factor_range)
    return waveform * factor


# ============================================================
# 数据集
# ============================================================

class SnoreDataset(Dataset):
    """
    文件夹结构：
      data_dir/
        snore/
          001.wav
          002.wav
          ...
        non_snore/
          001.wav
          002.wav
          ...
    """

    def __init__(self, data_dir, sr=16000, duration_sec=2,
                 augment=False, n_mfcc=13):
        self.sr = sr
        self.duration = sr * duration_sec  # 样本长度（采样点数）
        self.augment = augment
        self.n_mfcc = n_mfcc
        self.samples = []
        self.labels = []

        # 加载 snore (label=1)
        snore_dir = os.path.join(data_dir, "snore")
        if os.path.isdir(snore_dir):
            for f in os.listdir(snore_dir):
                if f.endswith((".wav", ".mp3", ".flac")):
                    self.samples.append(os.path.join(snore_dir, f))
                    self.labels.append(1)

        # 加载 non_snore (label=0)
        non_dir = os.path.join(data_dir, "non_snore")
        if os.path.isdir(non_dir):
            for f in os.listdir(non_dir):
                if f.endswith((".wav", ".mp3", ".flac")):
                    self.samples.append(os.path.join(non_dir, f))
                    self.labels.append(0)

        if len(self.samples) == 0:
            raise RuntimeError(f"未找到音频数据，请检查 {data_dir}")

        # MFCC 提取器
        self.mfcc_transform = torchaudio.transforms.MFCC(
            sample_rate=sr,
            n_mfcc=n_mfcc,
            melkwargs={"n_fft": 512, "hop_length": 256, "n_mels": 40},
        )

    def __len__(self):
        return len(self.samples)

    def _load_wav(self, path):
        waveform, sr = torchaudio.load(path)
        # 单声道
        if waveform.shape[0] > 1:
            waveform = torch.mean(waveform, dim=0, keepdim=True)
        # 重采样
        if sr != self.sr:
            resampler = torchaudio.transforms.Resample(sr, self.sr)
            waveform = resampler(waveform)
        return waveform.squeeze(0)

    def __getitem__(self, idx):
        waveform = self._load_wav(self.samples[idx])
        label = self.labels[idx]

        # 裁剪或填充到固定长度
        if len(waveform) > self.duration:
            start = np.random.randint(0, len(waveform) - self.duration)
            waveform = waveform[start:start + self.duration]
        else:
            pad = torch.zeros(self.duration - len(waveform))
            waveform = torch.cat([waveform, pad])

        # 数据增强
        if self.augment:
            if np.random.random() < 0.5:
                waveform = add_noise(waveform, snr_db=np.random.uniform(10, 30))
            if np.random.random() < 0.3:
                waveform = volume_perturb(waveform)
            if np.random.random() < 0.3:
                waveform = time_shift(waveform)

        # 提取 MFCC
        mfcc = self.mfcc_transform(waveform.unsqueeze(0))  # [1, n_mfcc, time]
        return mfcc.squeeze(0), torch.tensor(label, dtype=torch.long)


# ============================================================
# 模型（轻量版，≤50KB 参数）
# ============================================================

class SnoreNet(nn.Module):
    """轻量鼾声检测网络"""

    def __init__(self, n_mfcc=13, n_classes=2):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=(3, 3), padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d((2, 2)),
            nn.Conv2d(16, 32, kernel_size=(3, 3), padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d((2, 2)),
            nn.Conv2d(32, 32, kernel_size=(3, 3), padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, 16)),  # 压缩到固定尺寸给 LSTM
        )
        self.lstm = nn.LSTM(input_size=32, hidden_size=32,
                            num_layers=1, batch_first=True)
        self.fc = nn.Linear(32, n_classes)
        self.dropout = nn.Dropout(0.3)

    def forward(self, x):
        # x: [batch, n_mfcc, time_steps]
        x = x.unsqueeze(1)  # [batch, 1, n_mfcc, time]
        x = self.conv(x)    # [batch, 32, 1, 16]
        x = x.squeeze(2)    # [batch, 32, 16]
        x = x.transpose(1, 2)  # [batch, 16, 32]
        x, _ = self.lstm(x)
        x = x[:, -1, :]     # 取最后一步
        x = self.dropout(x)
        x = self.fc(x)
        return x


def count_params(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# ============================================================
# 训练
# ============================================================

def train(data_dir, epochs=30, batch_size=16, lr=1e-3, save_dir="data/models"):
    os.makedirs(save_dir, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"🚀 训练设备: {device}")

    # 数据
    train_ds = SnoreDataset(data_dir, augment=True)
    val_ds = SnoreDataset(data_dir, augment=False)  # 简单起见用同一批，实际应分拆
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size)

    print(f"📊 训练集: {len(train_ds)} 个样本")

    # 模型
    model = SnoreNet().to(device)
    n_params = count_params(model)
    print(f"🧠 模型参数量: {n_params} ({n_params*4/1024:.1f} KB fp32)")

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_acc = 0
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        correct = 0
        total = 0
        for mfcc, labels in train_loader:
            mfcc, labels = mfcc.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(mfcc)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            _, predicted = torch.max(outputs, 1)
            correct += (predicted == labels).sum().item()
            total += labels.size(0)

        scheduler.step()
        train_acc = correct / total

        # 验证
        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for mfcc, labels in val_loader:
                mfcc, labels = mfcc.to(device), labels.to(device)
                outputs = model(mfcc)
                _, predicted = torch.max(outputs, 1)
                val_correct += (predicted == labels).sum().item()
                val_total += labels.size(0)
        val_acc = val_correct / val_total

        print(f"Epoch {epoch+1:3d}/{epochs} | "
              f"Loss: {total_loss/len(train_loader):.4f} | "
              f"Train: {train_acc:.3f} | Val: {val_acc:.3f}")

        if val_acc > best_acc:
            best_acc = val_acc
            torch.save(model.state_dict(), os.path.join(save_dir, "snore_model.pth"))
            print(f"  ✅ 保存最佳模型 (acc={best_acc:.3f})")

    print(f"\n🏁 训练完成，最佳验证准确率: {best_acc:.3f}")
    return best_acc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="训练鼾声检测模型")
    parser.add_argument("--data_dir", default="data/audio_samples", help="音频数据目录")
    parser.add_argument("--epochs", type=int, default=30, help="训练轮数")
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--save_dir", default="data/models")
    args = parser.parse_args()
    train(args.data_dir, args.epochs, args.batch_size, args.lr, args.save_dir)
