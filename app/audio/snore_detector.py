"""
鼾声检测 —— 规则版基线（可直接跑，不依赖任何数据集）
策略：先规则版保底（100%可控），再模型版加分（可选）

检测维度（Arthur医学文档 + Bob硬件方案综合）：
  1. 音量阈值（RMS 超基线 10-15dB）
  2. 频谱能量分布（100-850Hz 占比 > 60%）
  3. 周期性（自相关峰在 0.8-3Hz 区间 = 48-180次/分呼吸率）
  4. 稳定性（连续多帧都满足）
"""
import numpy as np
from typing import Tuple


def compute_rms(signal: np.ndarray) -> float:
    """计算均方根（音量）"""
    if len(signal) == 0:
        return 0
    return np.sqrt(np.mean(signal ** 2))


def compute_snr_snore_band(signal: np.ndarray, sr: int = 16000) -> Tuple[float, float]:
    """
    计算鼾声频段能量占比
    鼾声主能量：100-850Hz（男性更低，女性稍高）
    返回：(鼾声频段能量占比, 峰值频率Hz)
    """
    n = len(signal)
    if n == 0:
        return 0, 0

    # FFT
    spectrum = np.abs(np.fft.fft(signal))[:n//2]
    freqs = np.fft.fftfreq(n, 1/sr)[:n//2]

    # 鼾声频段 100-850Hz
    snore_mask = (freqs >= 100) & (freqs <= 850)
    total_energy = np.sum(spectrum)
    if total_energy < 1e-9:
        return 0, 0

    snore_energy = np.sum(spectrum[snore_mask])
    snore_ratio = snore_energy / total_energy

    # 峰值频率
    peak_idx = np.argmax(spectrum[snore_mask]) if any(snore_mask) else 0
    peak_freq = freqs[snore_mask][peak_idx] if any(snore_mask) else 0

    return snore_ratio, peak_freq


def compute_periodicity(signal: np.ndarray, sr: int = 16000) -> Tuple[float, float]:
    """
    计算周期性（自相关法）
    呼吸/打鼾频率范围：0.2-3 Hz（12-180次/分）
    返回：(周期性强度 0-1, 基频Hz)
    """
    n = len(signal)
    if n < sr * 0.5:
        return 0, 0

    # 自相关
    autocorr = np.correlate(signal, signal, mode='full')
    autocorr = autocorr[n-1:]  # 取正延迟部分
    if autocorr[0] < 1e-9:
        return 0, 0
    autocorr = autocorr / autocorr[0]  # 归一化

    # 找呼吸/打鼾频率范围的峰值（对应 0.2-3 Hz）
    min_lag = int(sr / 3)     # 最高频率 3Hz = 0.33秒/周期
    max_lag = int(sr / 0.2)   # 最低频率 0.2Hz = 5秒/周期

    if min_lag >= len(autocorr) or max_lag >= len(autocorr):
        return 0, 0

    search_range = autocorr[min_lag:max_lag+1]
    if len(search_range) == 0:
        return 0, 0

    peak_idx = np.argmax(search_range)
    peak_val = search_range[peak_idx]
    peak_lag = min_lag + peak_idx
    fundamental_freq = sr / peak_lag if peak_lag > 0 else 0

    return peak_val, fundamental_freq


class RuleBasedSnoreDetector:
    """
    规则版鼾声检测器

    综合置信度 = 0.35*频段得分 + 0.35*周期得分 + 0.2*音量得分 + 0.1*稳定性得分
    """

    def __init__(self, sr: int = 16000, baseline_rms: float = None):
        self.sr = sr
        self.baseline_rms = baseline_rms or 0.001
        self.history = []  # 最近几帧的置信度，用于稳定性判断
        self.history_size = 5

    def set_baseline(self, rms: float):
        """设置环境基线音量"""
        self.baseline_rms = max(rms, 0.0001)

    def detect(self, frame: np.ndarray) -> Tuple[bool, float, dict]:
        """
        检测一帧音频是否为鼾声
        返回：(是否鼾声, 置信度 0-1, 详细特征)
        """
        if len(frame) == 0:
            return False, 0, {}

        # 1. 音量
        rms = compute_rms(frame)
        volume_ratio = rms / self.baseline_rms if self.baseline_rms > 0 else 0
        volume_score = min(volume_ratio / 3.0, 1.0)  # 超基线3倍即满分

        # 2. 频谱（鼾声频段占比）
        snore_ratio, peak_freq = compute_snr_snore_band(frame, self.sr)
        spectrum_score = min(snore_ratio / 0.6, 1.0)  # 60%以上即满分

        # 3. 周期性
        periodicity, fund_freq = compute_periodicity(frame, self.sr)
        # 只有基频在合理范围内才算分
        if 0.5 <= fund_freq <= 2.5:  # 30-150次/分
            period_score = periodicity
        else:
            period_score = 0

        # 4. 稳定性（历史加权）
        current_raw = 0.35 * spectrum_score + 0.35 * period_score + 0.2 * volume_score + 0.1
        self.history.append(current_raw)
        if len(self.history) > self.history_size:
            self.history.pop(0)
        stability = min(len(self.history) / self.history_size, 1.0)
        avg_confidence = np.mean(self.history) if self.history else 0
        stability_score = stability * 0.5  # 最多加0.5分

        # 综合置信度
        base_confidence = 0.35 * spectrum_score + 0.35 * period_score + 0.2 * volume_score
        confidence = base_confidence + stability_score * 0.1  # 稳定性加分
        confidence = min(max(confidence, 0), 1.0)

        is_snore = confidence >= 0.6

        features = {
            "rms": round(rms, 6),
            "volume_ratio": round(volume_ratio, 2),
            "snore_band_ratio": round(snore_ratio, 3),
            "peak_freq_hz": round(peak_freq, 1),
            "periodicity": round(periodicity, 3),
            "fundamental_freq_hz": round(fund_freq, 2),
            "volume_score": round(volume_score, 3),
            "spectrum_score": round(spectrum_score, 3),
            "period_score": round(period_score, 3),
            "stability_frames": len(self.history),
            "avg_confidence": round(avg_confidence, 3),
        }

        return is_snore, confidence, features
