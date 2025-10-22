"""
Time-Series Data Loader for ML Training

Loads historical OHLCV data from TimescaleDB, creates sliding window sequences,
and prepares batches for GPU training with PyTorch.

Features:
- Chronological train/val/test split (no look-ahead bias)
- Sliding window sequences for time-series modeling
- Multi-timeframe dataset support (5min, 15min, 1hr, 12hr, 1day)
- Efficient batching for GPU training
- Data augmentation (optional): time warping, magnitude warping, SMOTE

References:
- PyTorch Data Loading: https://pytorch.org/tutorials/beginner/basics/data_tutorial.html
- Time-Series CV: https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split
"""

import logging
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader
import psycopg2
from psycopg2.extras import RealDictCursor

# Project imports
from data.storage.timescale_writer import TimescaleWriter
from config.config_manager import get_config

logger = logging.getLogger(__name__)


class TimeSeriesDataset(Dataset):
    """
    PyTorch Dataset for time-series OHLCV data with sliding windows.
    
    Creates sequences of length `sequence_length` from historical data,
    suitable for training Transformer models or LSTMs.
    
    Example:
        dataset = TimeSeriesDataset(
            data=df,  # DataFrame with OHLCV columns
            sequence_length=60,  # 60 bars per sequence
            prediction_horizon=1,  # Predict 1 step ahead
            features=['open', 'high', 'low', 'close', 'volume']
        )
        
        x, y = dataset[0]  # Get first sequence
        # x: [60, 5] tensor, y: [1, 5] tensor
    """
    
    def __init__(
        self,
        data: pd.DataFrame,
        sequence_length: int = 60,
        prediction_horizon: int = 1,
        features: Optional[List[str]] = None,
        target: str = 'close',
        normalize: bool = True
    ):
        """
        Initialize time-series dataset.
        
        Args:
            data: DataFrame with columns: [time, ticker, open, high, low, close, volume, ...]
            sequence_length: Number of timesteps per sequence (input window)
            prediction_horizon: Number of timesteps to predict ahead
            features: List of feature column names (default: OHLCV)
            target: Target column to predict (default: 'close')
            normalize: Whether to normalize features per sequence
        """
        self.data = data.copy()
        self.sequence_length = sequence_length
        self.prediction_horizon = prediction_horizon
        self.features = features or ['open', 'high', 'low', 'close', 'volume']
        self.target = target
        self.normalize = normalize
        
        # Ensure data is sorted by time
        if 'time' in self.data.columns:
            self.data = self.data.sort_values('time').reset_index(drop=True)
        
        # Calculate valid sequence indices
        self.valid_indices = self._get_valid_indices()
        
        # Store normalization stats (mean, std per feature)
        if self.normalize:
            self.feature_means = self.data[self.features].mean()
            self.feature_stds = self.data[self.features].std() + 1e-8
        
        logger.info(f"TimeSeriesDataset initialized:")
        logger.info(f"  Total bars: {len(self.data)}")
        logger.info(f"  Valid sequences: {len(self.valid_indices)}")
        logger.info(f"  Sequence length: {self.sequence_length}")
        logger.info(f"  Prediction horizon: {self.prediction_horizon}")
        logger.info(f"  Features: {self.features}")
    
    def _get_valid_indices(self) -> List[int]:
        """
        Calculate valid starting indices for sequences.
        
        A valid index i must satisfy:
        - i + sequence_length + prediction_horizon <= len(data)
        - If ticker column exists, ensure no ticker change within sequence
        
        Returns:
            List of valid starting indices
        """
        valid_indices = []
        max_idx = len(self.data) - self.sequence_length - self.prediction_horizon + 1
        
        if 'ticker' in self.data.columns:
            # Ensure no ticker change within sequence
            for i in range(max_idx):
                end_idx = i + self.sequence_length + self.prediction_horizon
                tickers_in_window = self.data.loc[i:end_idx-1, 'ticker'].unique()
                if len(tickers_in_window) == 1:
                    valid_indices.append(i)
        else:
            # No ticker column, all indices valid
            valid_indices = list(range(max_idx))
        
        return valid_indices
    
    def __len__(self) -> int:
        """Return number of valid sequences."""
        return len(self.valid_indices)
    
    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Get a single sequence sample.
        
        Args:
            idx: Index of the sequence
        
        Returns:
            x: Input sequence tensor [sequence_length, n_features]
            y: Target sequence tensor [prediction_horizon, 1] or [prediction_horizon, n_features]
        """
        # Get actual data index
        start_idx = self.valid_indices[idx]
        end_idx = start_idx + self.sequence_length
        target_idx = end_idx + self.prediction_horizon - 1
        
        # Extract features
        x_data = self.data.loc[start_idx:end_idx-1, self.features].values
        y_data = self.data.loc[target_idx:target_idx, self.target].values
        
        # Normalize if enabled
        if self.normalize:
            x_data = (x_data - self.feature_means.values) / self.feature_stds.values
        
        # Convert to tensors
        x = torch.FloatTensor(x_data)  # [sequence_length, n_features]
        y = torch.FloatTensor(y_data)  # [1]
        
        return x, y
    
    def get_stats(self) -> Dict:
        """Get dataset statistics."""
        return {
            'total_bars': len(self.data),
            'valid_sequences': len(self.valid_indices),
            'sequence_length': self.sequence_length,
            'prediction_horizon': self.prediction_horizon,
            'n_features': len(self.features),
            'date_range': (
                self.data['time'].min() if 'time' in self.data.columns else None,
                self.data['time'].max() if 'time' in self.data.columns else None
            )
        }


class TimeSeriesDataLoader:
    """
    High-level data loader for training ML models on market data.
    
    Handles:
    - Loading data from TimescaleDB
    - Chronological train/val/test split (70/15/15)
    - Creating PyTorch DataLoaders for each split
    - Multi-timeframe dataset management
    
    Example:
        loader = TimeSeriesDataLoader(
            tickers=['AAPL', 'TSLA', 'NVDA'],
            start_date='2020-01-01',
            end_date='2024-12-31',
            timeframe='15min',
            sequence_length=60
        )
        
        train_loader, val_loader, test_loader = loader.get_data_loaders(
            batch_size=32,
            num_workers=4
        )
        
        # Training loop
        for x_batch, y_batch in train_loader:
            # x_batch: [batch_size, sequence_length, n_features]
            # y_batch: [batch_size, 1]
            predictions = model(x_batch)
            loss = criterion(predictions, y_batch)
            ...
    """
    
    def __init__(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        timeframe: str = '15min',
        sequence_length: int = 60,
        prediction_horizon: int = 1,
        features: Optional[List[str]] = None,
        target: str = 'close',
        train_ratio: float = 0.70,
        val_ratio: float = 0.15,
        test_ratio: float = 0.15,
        db_writer: Optional[TimescaleWriter] = None
    ):
        """
        Initialize data loader.
        
        Args:
            tickers: List of ticker symbols to load
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            timeframe: Bar timeframe ('1min', '5min', '15min', '1hour', '12hour', '1day')
            sequence_length: Number of bars per input sequence
            prediction_horizon: Number of steps ahead to predict
            features: List of feature columns (default: OHLCV)
            target: Target column to predict
            train_ratio: Proportion of data for training (default: 0.70)
            val_ratio: Proportion of data for validation (default: 0.15)
            test_ratio: Proportion of data for testing (default: 0.15)
            db_writer: TimescaleDB writer instance (creates new if None)
        """
        assert abs(train_ratio + val_ratio + test_ratio - 1.0) < 1e-6, "Split ratios must sum to 1.0"
        
        self.tickers = tickers
        self.start_date = start_date
        self.end_date = end_date
        self.timeframe = timeframe
        self.sequence_length = sequence_length
        self.prediction_horizon = prediction_horizon
        self.features = features or ['open', 'high', 'low', 'close', 'volume']
        self.target = target
        self.train_ratio = train_ratio
        self.val_ratio = val_ratio
        self.test_ratio = test_ratio
        
        # Initialize database connection
        self.db_writer = db_writer or TimescaleWriter()
        
        # Load data
        logger.info(f"Loading data for {len(tickers)} tickers from {start_date} to {end_date}")
        self.data = self._load_data()
        
        # Create train/val/test splits
        logger.info(f"Creating chronological splits: train={train_ratio}, val={val_ratio}, test={test_ratio}")
        self.train_data, self.val_data, self.test_data = self._create_splits()
        
        logger.info(f"Data loader initialized:")
        logger.info(f"  Train samples: {len(self.train_data)}")
        logger.info(f"  Val samples: {len(self.val_data)}")
        logger.info(f"  Test samples: {len(self.test_data)}")
    
    def _load_data(self) -> pd.DataFrame:
        """
        Load historical OHLCV data from TimescaleDB.
        
        Returns:
            DataFrame with columns: [time, ticker, open, high, low, close, volume]
        """
        try:
            with self.db_writer.get_connection() as conn:
                cursor = conn.cursor(cursor_factory=RealDictCursor)
                
                # Build query
                query = """
                    SELECT 
                        time,
                        ticker,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        vwap
                    FROM market_data
                    WHERE ticker = ANY(%s)
                      AND timeframe = %s
                      AND time BETWEEN %s AND %s
                    ORDER BY ticker, time ASC
                """
                
                cursor.execute(query, (self.tickers, self.timeframe, self.start_date, self.end_date))
                rows = cursor.fetchall()
                
                if not rows:
                    logger.warning(f"No data found for tickers {self.tickers} in timeframe {self.timeframe}")
                    return pd.DataFrame(columns=['time', 'ticker', 'open', 'high', 'low', 'close', 'volume'])
                
                df = pd.DataFrame(rows)
                logger.info(f"Loaded {len(df):,} bars from database")
                
                return df
                
        except psycopg2.Error as e:
            logger.error(f"Database error loading data: {e}")
            raise
    
    def _create_splits(self) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Create chronological train/val/test splits.
        
        Ensures no look-ahead bias by splitting strictly by time.
        If multiple tickers, splits each ticker separately then concatenates.
        
        Returns:
            train_df, val_df, test_df
        """
        if 'ticker' in self.data.columns and len(self.tickers) > 1:
            # Split each ticker separately
            train_dfs, val_dfs, test_dfs = [], [], []
            
            for ticker in self.tickers:
                ticker_data = self.data[self.data['ticker'] == ticker].reset_index(drop=True)
                
                n = len(ticker_data)
                train_end = int(n * self.train_ratio)
                val_end = int(n * (self.train_ratio + self.val_ratio))
                
                train_dfs.append(ticker_data.iloc[:train_end])
                val_dfs.append(ticker_data.iloc[train_end:val_end])
                test_dfs.append(ticker_data.iloc[val_end:])
            
            train_df = pd.concat(train_dfs, ignore_index=True)
            val_df = pd.concat(val_dfs, ignore_index=True)
            test_df = pd.concat(test_dfs, ignore_index=True)
        
        else:
            # Single ticker or no ticker column
            n = len(self.data)
            train_end = int(n * self.train_ratio)
            val_end = int(n * (self.train_ratio + self.val_ratio))
            
            train_df = self.data.iloc[:train_end].reset_index(drop=True)
            val_df = self.data.iloc[train_end:val_end].reset_index(drop=True)
            test_df = self.data.iloc[val_end:].reset_index(drop=True)
        
        return train_df, val_df, test_df
    
    def get_data_loaders(
        self,
        batch_size: int = 32,
        num_workers: int = 4,
        shuffle_train: bool = True,
        normalize: bool = True
    ) -> Tuple[DataLoader, DataLoader, DataLoader]:
        """
        Create PyTorch DataLoaders for train/val/test splits.
        
        Args:
            batch_size: Batch size for training
            num_workers: Number of worker processes for data loading
            shuffle_train: Whether to shuffle training data (not recommended for time-series)
            normalize: Whether to normalize features
        
        Returns:
            train_loader, val_loader, test_loader
        """
        # Create datasets
        train_dataset = TimeSeriesDataset(
            self.train_data,
            sequence_length=self.sequence_length,
            prediction_horizon=self.prediction_horizon,
            features=self.features,
            target=self.target,
            normalize=normalize
        )
        
        val_dataset = TimeSeriesDataset(
            self.val_data,
            sequence_length=self.sequence_length,
            prediction_horizon=self.prediction_horizon,
            features=self.features,
            target=self.target,
            normalize=normalize
        )
        
        test_dataset = TimeSeriesDataset(
            self.test_data,
            sequence_length=self.sequence_length,
            prediction_horizon=self.prediction_horizon,
            features=self.features,
            target=self.target,
            normalize=normalize
        )
        
        # Create data loaders
        train_loader = DataLoader(
            train_dataset,
            batch_size=batch_size,
            shuffle=shuffle_train,  # Usually False for time-series
            num_workers=num_workers,
            pin_memory=True  # Faster GPU transfer
        )
        
        val_loader = DataLoader(
            val_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
            pin_memory=True
        )
        
        test_loader = DataLoader(
            test_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
            pin_memory=True
        )
        
        return train_loader, val_loader, test_loader
    
    def get_stats(self) -> Dict:
        """Get dataset statistics."""
        return {
            'tickers': self.tickers,
            'timeframe': self.timeframe,
            'date_range': (self.start_date, self.end_date),
            'total_bars': len(self.data),
            'train_bars': len(self.train_data),
            'val_bars': len(self.val_data),
            'test_bars': len(self.test_data),
            'sequence_length': self.sequence_length,
            'prediction_horizon': self.prediction_horizon,
            'features': self.features
        }


def create_multi_timeframe_loaders(
    tickers: List[str],
    start_date: str,
    end_date: str,
    timeframes: Optional[List[str]] = None,
    **kwargs
) -> Dict[str, TimeSeriesDataLoader]:
    """
    Create data loaders for multiple timeframes.
    
    Args:
        tickers: List of ticker symbols
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        timeframes: List of timeframes (default: ['5min', '15min', '1hour', '12hour'])
        **kwargs: Additional arguments passed to TimeSeriesDataLoader
    
    Returns:
        Dict mapping timeframe -> TimeSeriesDataLoader
    
    Example:
        loaders = create_multi_timeframe_loaders(
            tickers=['AAPL', 'TSLA'],
            start_date='2020-01-01',
            end_date='2024-12-31',
            timeframes=['15min', '1hour'],
            sequence_length=60
        )
        
        train_15min, val_15min, test_15min = loaders['15min'].get_data_loaders()
        train_1hour, val_1hour, test_1hour = loaders['1hour'].get_data_loaders()
    """
    timeframes = timeframes or ['5min', '15min', '1hour', '12hour']
    
    loaders = {}
    for tf in timeframes:
        logger.info(f"Creating data loader for timeframe: {tf}")
        loaders[tf] = TimeSeriesDataLoader(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            timeframe=tf,
            **kwargs
        )
    
    return loaders


if __name__ == '__main__':
    # Test data loader
    logging.basicConfig(level=logging.INFO)
    
    # Example usage
    loader = TimeSeriesDataLoader(
        tickers=['AAPL'],
        start_date='2024-01-01',
        end_date='2024-10-01',
        timeframe='15min',
        sequence_length=60,
        prediction_horizon=1
    )
    
    train_loader, val_loader, test_loader = loader.get_data_loaders(batch_size=32)
    
    print("\n=== Data Loader Stats ===")
    print(loader.get_stats())
    
    print("\n=== Sample Batch ===")
    for x, y in train_loader:
        print(f"Input shape: {x.shape}")   # [batch_size, sequence_length, n_features]
        print(f"Target shape: {y.shape}")  # [batch_size, 1]
        print(f"Input sample: {x[0, :5, :]}")  # First 5 timesteps of first sample
        print(f"Target sample: {y[0]}")
        break
