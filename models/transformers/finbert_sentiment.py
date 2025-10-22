"""
FinBERT Sentiment Analysis

Implementation of FinBERT (Financial BERT) for sentiment analysis of financial text.

FinBERT is a pre-trained NLP model to analyze sentiment of financial text. It is built
by further training the BERT language model in the finance domain, using a large financial
corpus and thereby fine-tuning it for financial sentiment classification.

Features:
- Pre-trained FinBERT from Hugging Face (ProsusAI/finbert)
- Sentiment classification: positive/negative/neutral
- Batch processing for efficiency
- GPU acceleration
- Sentiment score normalization to [-1, 1]

References:
- FinBERT Paper: "FinBERT: Financial Sentiment Analysis with Pre-trained Language Models"
  https://arxiv.org/abs/1908.10063
- Hugging Face Model: https://huggingface.co/ProsusAI/finbert
- BERT: "BERT: Pre-training of Deep Bidirectional Transformers" (Devlin et al., 2018)

Example:
    analyzer = FinBERTSentiment()
    
    texts = [
        "Apple stock surges on strong earnings report",
        "Tesla faces regulatory challenges in China",
        "Fed maintains interest rates, markets stable"
    ]
    
    results = analyzer.analyze_batch(texts)
    # [{'text': '...', 'sentiment': 'positive', 'score': 0.85, 'confidence': 0.92}, ...]
"""

import logging
from typing import List, Dict, Optional, Union
import numpy as np

# Note: These imports will work once transformers package is installed
# pip install transformers torch
try:
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    import torch.nn.functional as F
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logging.warning("transformers or torch not installed. FinBERT will not be available.")

logger = logging.getLogger(__name__)


class FinBERTSentiment:
    """
    FinBERT-based sentiment analyzer for financial text.
    
    Analyzes sentiment of financial news, reports, and social media.
    Returns sentiment classification (positive/negative/neutral) and
    normalized sentiment scores in range [-1, 1].
    
    Args:
        model_name: Hugging Face model name (default: 'ProsusAI/finbert')
        device: Device to run inference on ('cuda', 'cpu', or None for auto)
        batch_size: Batch size for processing multiple texts (default: 32)
        max_length: Maximum token length (default: 512)
    
    Example:
        analyzer = FinBERTSentiment(device='cuda')
        
        # Single text
        result = analyzer.analyze("Apple reports record quarterly revenue")
        # {'sentiment': 'positive', 'score': 0.78, 'confidence': 0.89, ...}
        
        # Batch processing
        texts = ["Stock surges...", "Market crashes...", "Earnings meet expectations..."]
        results = analyzer.analyze_batch(texts)
    """
    
    def __init__(
        self,
        model_name: str = 'ProsusAI/finbert',
        device: Optional[str] = None,
        batch_size: int = 32,
        max_length: int = 512
    ):
        """Initialize FinBERT sentiment analyzer."""
        
        if not TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "transformers and torch are required for FinBERT. "
                "Install with: pip install transformers torch"
            )
        
        self.model_name = model_name
        self.batch_size = batch_size
        self.max_length = max_length
        
        # Set device
        if device is None:
            self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self.device = device
        
        logger.info(f"Initializing FinBERT on device: {self.device}")
        
        # Load tokenizer and model
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
            self.model.to(self.device)
            self.model.eval()  # Set to evaluation mode
            
            logger.info(f"✓ FinBERT loaded successfully: {model_name}")
            
        except Exception as e:
            logger.error(f"Failed to load FinBERT model: {e}")
            raise
        
        # Label mapping (FinBERT uses these labels)
        # 0: positive, 1: negative, 2: neutral
        self.id2label = {0: 'positive', 1: 'negative', 2: 'neutral'}
        self.label2id = {'positive': 0, 'negative': 1, 'neutral': 2}
    
    def analyze(self, text: str) -> Dict[str, Union[str, float]]:
        """
        Analyze sentiment of a single text.
        
        Args:
            text: Financial text to analyze
        
        Returns:
            Dictionary with:
                - sentiment: Label ('positive', 'negative', 'neutral')
                - score: Normalized sentiment score [-1, 1]
                - confidence: Model confidence [0, 1]
                - probabilities: Dict of class probabilities
                - text: Original input text
        """
        results = self.analyze_batch([text])
        return results[0]
    
    def analyze_batch(
        self,
        texts: List[str],
        return_all_scores: bool = False
    ) -> List[Dict[str, Union[str, float]]]:
        """
        Analyze sentiment of multiple texts in batches.
        
        Args:
            texts: List of financial texts to analyze
            return_all_scores: If True, return probabilities for all classes
        
        Returns:
            List of sentiment analysis results
        """
        if not texts:
            return []
        
        results = []
        
        # Process in batches for efficiency
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i:i + self.batch_size]
            batch_results = self._process_batch(batch_texts, return_all_scores)
            results.extend(batch_results)
        
        return results
    
    def _process_batch(
        self,
        texts: List[str],
        return_all_scores: bool = False
    ) -> List[Dict[str, Union[str, float]]]:
        """
        Process a single batch of texts.
        
        Args:
            texts: Batch of texts
            return_all_scores: Return all class probabilities
        
        Returns:
            List of sentiment results for batch
        """
        # Tokenize batch
        inputs = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors='pt'
        )
        
        # Move to device
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        
        # Run inference (no gradient computation needed)
        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits
        
        # Get probabilities
        probabilities = F.softmax(logits, dim=-1).cpu().numpy()
        
        # Get predictions
        predictions = np.argmax(probabilities, axis=-1)
        
        # Format results
        results = []
        for idx, (text, pred_id, probs) in enumerate(zip(texts, predictions, probabilities)):
            sentiment = self.id2label[pred_id]
            confidence = float(probs[pred_id])
            
            # Calculate normalized sentiment score [-1, 1]
            # positive: +score, negative: -score, neutral: 0
            if sentiment == 'positive':
                score = float(probs[0])  # Use positive probability
            elif sentiment == 'negative':
                score = -float(probs[1])  # Use negative probability (negated)
            else:  # neutral
                # Neutral score is weighted average
                score = float(probs[0] - probs[1])  # positive - negative
            
            result = {
                'text': text,
                'sentiment': sentiment,
                'score': score,  # Range: [-1, 1]
                'confidence': confidence,
            }
            
            # Add all class probabilities if requested
            if return_all_scores:
                result['probabilities'] = {
                    'positive': float(probs[0]),
                    'negative': float(probs[1]),
                    'neutral': float(probs[2])
                }
            
            results.append(result)
        
        return results
    
    def get_sentiment_score(self, text: str) -> float:
        """
        Get normalized sentiment score for a single text.
        
        Convenience method that returns just the score [-1, 1].
        
        Args:
            text: Financial text
        
        Returns:
            Sentiment score in range [-1, 1]
                +1.0: Very positive
                 0.0: Neutral
                -1.0: Very negative
        """
        result = self.analyze(text)
        score = result['score']
        return float(score) if isinstance(score, (int, float)) else 0.0
    
    def analyze_news_batch(
        self,
        news_items: List[Dict[str, str]]
    ) -> List[Dict]:
        """
        Analyze sentiment of news articles.
        
        Processes news items with title and description/summary.
        
        Args:
            news_items: List of dicts with 'title' and optionally 'description'
        
        Returns:
            List of news items with sentiment fields added
        
        Example:
            news = [
                {'title': 'Apple hits all-time high', 'description': 'Strong earnings...'},
                {'title': 'Market uncertainty continues', 'description': 'Investors worried...'}
            ]
            
            results = analyzer.analyze_news_batch(news)
            # Results include original fields + sentiment, score, confidence
        """
        # Combine title and description for better context
        texts = []
        for item in news_items:
            title = item.get('title', '')
            description = item.get('description', '')
            
            # Combine title and description
            if description:
                combined = f"{title}. {description}"
            else:
                combined = title
            
            texts.append(combined)
        
        # Analyze sentiments
        sentiments = self.analyze_batch(texts, return_all_scores=True)
        
        # Merge with original news items
        results = []
        for item, sentiment in zip(news_items, sentiments):
            result = item.copy()
            result['sentiment'] = sentiment['sentiment']
            result['sentiment_score'] = sentiment['score']
            result['sentiment_confidence'] = sentiment['confidence']
            result['sentiment_probabilities'] = sentiment.get('probabilities', {})
            results.append(result)
        
        return results
    
    def benchmark(self, num_texts: int = 100, text_length: int = 100) -> Dict[str, float]:
        """
        Benchmark inference speed.
        
        Args:
            num_texts: Number of sample texts to process
            text_length: Approximate length of each text (characters)
        
        Returns:
            Performance metrics (texts/sec, batches/sec, etc.)
        """
        import time
        
        # Generate sample texts
        sample_text = "The stock market " * (text_length // 20)
        texts = [sample_text] * num_texts
        
        logger.info(f"Benchmarking FinBERT on {num_texts} texts...")
        
        # Warmup
        self.analyze_batch(texts[:10])
        
        # Timed run
        start_time = time.time()
        results = self.analyze_batch(texts)
        elapsed_time = time.time() - start_time
        
        texts_per_sec = num_texts / elapsed_time
        batches_per_sec = (num_texts / self.batch_size) / elapsed_time
        
        metrics = {
            'num_texts': num_texts,
            'elapsed_seconds': elapsed_time,
            'texts_per_second': texts_per_sec,
            'batches_per_second': batches_per_sec,
            'batch_size': self.batch_size,
            'device': self.device
        }
        
        logger.info(f"Benchmark results:")
        logger.info(f"  Texts/sec: {texts_per_sec:.2f}")
        logger.info(f"  Batches/sec: {batches_per_sec:.2f}")
        logger.info(f"  Device: {self.device}")
        
        return metrics


class SentimentAggregator:
    """
    Aggregates sentiment scores over time windows or across multiple sources.
    
    Useful for:
    - Computing rolling sentiment for a stock
    - Combining news sentiment from multiple sources
    - Tracking sentiment trends
    
    Example:
        aggregator = SentimentAggregator()
        
        # Add sentiments over time
        aggregator.add_sentiment(0.8, timestamp='2024-01-01 09:00')
        aggregator.add_sentiment(0.6, timestamp='2024-01-01 10:00')
        aggregator.add_sentiment(-0.3, timestamp='2024-01-01 11:00')
        
        # Get rolling average
        avg_sentiment = aggregator.get_rolling_average(window_hours=24)
    """
    
    def __init__(self):
        """Initialize sentiment aggregator."""
        self.sentiments = []
        self.timestamps = []
    
    def add_sentiment(
        self,
        score: float,
        timestamp: Optional[str] = None,
        weight: float = 1.0
    ):
        """
        Add a sentiment score.
        
        Args:
            score: Sentiment score [-1, 1]
            timestamp: Optional timestamp (ISO format or datetime object)
            weight: Weight for this sentiment (default: 1.0)
        """
        self.sentiments.append({
            'score': score,
            'timestamp': timestamp,
            'weight': weight
        })
    
    def get_average(self, weighted: bool = True) -> float:
        """
        Get average sentiment score.
        
        Args:
            weighted: Use weighted average
        
        Returns:
            Average sentiment score
        """
        if not self.sentiments:
            return 0.0
        
        if weighted:
            total_weight = sum(s['weight'] for s in self.sentiments)
            if total_weight == 0:
                return 0.0
            return sum(s['score'] * s['weight'] for s in self.sentiments) / total_weight
        else:
            return sum(s['score'] for s in self.sentiments) / len(self.sentiments)
    
    def get_trend(self, recent_n: int = 10) -> str:
        """
        Detect sentiment trend (improving/declining/stable).
        
        Args:
            recent_n: Number of recent sentiments to compare
        
        Returns:
            'improving', 'declining', or 'stable'
        """
        if len(self.sentiments) < 2:
            return 'stable'
        
        recent = self.sentiments[-recent_n:]
        
        if len(recent) < 2:
            return 'stable'
        
        # Compare first half vs second half
        mid = len(recent) // 2
        first_half_avg = sum(s['score'] for s in recent[:mid]) / mid
        second_half_avg = sum(s['score'] for s in recent[mid:]) / (len(recent) - mid)
        
        diff = second_half_avg - first_half_avg
        
        if diff > 0.1:
            return 'improving'
        elif diff < -0.1:
            return 'declining'
        else:
            return 'stable'


if __name__ == '__main__':
    # Test FinBERT
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if not TRANSFORMERS_AVAILABLE:
        print("ERROR: transformers package not installed.")
        print("Install with: pip install transformers torch")
        sys.exit(1)
    
    print("=" * 70)
    print("Testing FinBERT Sentiment Analysis")
    print("=" * 70)
    
    # Initialize analyzer
    analyzer = FinBERTSentiment()
    
    # Test samples
    test_texts = [
        "Apple stock surges on strong quarterly earnings beat",
        "Tesla faces challenges with supply chain disruptions",
        "Market remains stable as Fed holds interest rates steady",
        "NVIDIA reports record revenue driven by AI chip demand",
        "Banking sector under pressure amid regulatory concerns",
        "Tech stocks rally on positive economic outlook",
    ]
    
    print("\n" + "=" * 70)
    print("Analyzing Sample Headlines")
    print("=" * 70)
    
    results = analyzer.analyze_batch(test_texts, return_all_scores=True)
    
    for result in results:
        print(f"\nText: {result['text']}")
        sentiment = result['sentiment']
        print(f"Sentiment: {str(sentiment).upper()}")
        print(f"Score: {result['score']:.3f} (confidence: {result['confidence']:.3f})")
        print(f"Probabilities: {result['probabilities']}")
    
    # Benchmark
    print("\n" + "=" * 70)
    print("Benchmarking Performance")
    print("=" * 70)
    
    metrics = analyzer.benchmark(num_texts=100)
    
    print("\n" + "=" * 70)
    print("✓ FinBERT test completed successfully!")
    print("=" * 70)
