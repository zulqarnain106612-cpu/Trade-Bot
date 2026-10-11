"""Torch-backed LSTM network, imported only after the runtime memory guard passes."""

from torch import nn


class LSTMNet(nn.Module):
    """Small sequence model used by the optional ensemble LSTM member."""

    def __init__(self, input_size: int, hidden_dim: int = 64) -> None:
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_dim, batch_first=True)
        self.fc = nn.Linear(hidden_dim, 1)

    def forward(self, x):
        _, (hidden, _) = self.lstm(x)
        return self.fc(hidden[-1])
