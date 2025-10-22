"""
Event Impact Analyzer

Analyzes major market events and their potential impact on stocks/sectors.

Features:
- Event type classification (Fed decisions, tariffs, regulations, earnings, etc.)
- Impact severity assessment (0-1 scale)
- Affected asset/sector identification
- Impact timeframe estimation (short/medium/long-term)
- Volatility spike prediction
- Historical event pattern matching

Event Types:
- MONETARY_POLICY: Fed rate decisions, QE announcements
- GEOPOLITICAL: Trade wars, sanctions, international conflicts
- REGULATORY: New regulations, policy changes
- EARNINGS: Major earnings surprises
- ECONOMIC_DATA: GDP, inflation, employment reports
- MARKET_STRUCTURE: Circuit breakers, trading halts
- CORPORATE: Mergers, acquisitions, CEO changes
- CRISIS: Black swan events, market crashes

References:
- Event Study Methodology: https://en.wikipedia.org/wiki/Event_study
- Market Microstructure: O'Hara (1995)
"""

import logging
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timedelta
from enum import Enum
import re

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Major event categories that impact markets."""
    MONETARY_POLICY = "monetary_policy"
    GEOPOLITICAL = "geopolitical"
    REGULATORY = "regulatory"
    EARNINGS = "earnings"
    ECONOMIC_DATA = "economic_data"
    MARKET_STRUCTURE = "market_structure"
    CORPORATE = "corporate"
    CRISIS = "crisis"
    CRYPTO_REGULATION = "crypto_regulation"
    TECHNOLOGY = "technology"
    UNKNOWN = "unknown"


class ImpactTimeframe(Enum):
    """Expected duration of event impact."""
    IMMEDIATE = "immediate"     # Minutes to hours
    SHORT = "short"             # Hours to days
    MEDIUM = "medium"           # Days to weeks
    LONG = "long"               # Weeks to months
    STRUCTURAL = "structural"   # Permanent change


class EventImpactAnalyzer:
    """
    Analyzes events and predicts their market impact.
    
    Uses pattern matching, keyword detection, and historical patterns
    to assess event significance and identify affected assets.
    
    Example:
        analyzer = EventImpactAnalyzer()
        
        event = {
            'title': 'Fed raises interest rates by 75 basis points',
            'description': 'Federal Reserve announces aggressive rate hike...',
            'timestamp': '2024-01-15 14:00:00'
        }
        
        impact = analyzer.analyze_event(event)
        # {
        #     'event_type': 'monetary_policy',
        #     'impact_score': 0.85,
        #     'affected_sectors': ['financials', 'real_estate', 'utilities'],
        #     'timeframe': 'medium',
        #     'volatility_spike_expected': True,
        #     ...
        # }
    """
    
    def __init__(self):
        """Initialize event impact analyzer with pattern databases."""
        
        # Event type detection patterns (keywords/phrases)
        self.event_patterns = {
            EventType.MONETARY_POLICY: [
                r'\bfed\b', r'\bfomc\b', r'interest rate', r'rate hike', r'rate cut',
                r'quantitative easing', r'qe\b', r'monetary policy', r'powell',
                r'central bank', r'federal reserve', r'basis points', r'bps\b'
            ],
            EventType.GEOPOLITICAL: [
                r'tariff', r'trade war', r'sanction', r'embargo', r'conflict',
                r'geopolitical', r'war\b', r'military', r'china.*us', r'us.*china',
                r'brexit', r'election', r'political'
            ],
            EventType.REGULATORY: [
                r'regulation', r'sec\b', r'fda approval', r'antitrust', r'compliance',
                r'regulatory', r'investigation', r'fine\b', r'lawsuit', r'settlement',
                r'ban\b', r'restrict'
            ],
            EventType.EARNINGS: [
                r'earnings', r'quarterly results', r'eps\b', r'revenue beat',
                r'earnings miss', r'guidance', r'profit', r'loss\b'
            ],
            EventType.ECONOMIC_DATA: [
                r'gdp\b', r'unemployment', r'jobs report', r'inflation', r'cpi\b',
                r'ppi\b', r'retail sales', r'manufacturing', r'housing starts',
                r'consumer confidence', r'pmi\b'
            ],
            EventType.MARKET_STRUCTURE: [
                r'circuit breaker', r'trading halt', r'delisting', r'stock split',
                r'reverse split', r'ipo\b', r'initial public offering'
            ],
            EventType.CORPORATE: [
                r'merger', r'acquisition', r'ceo\b', r'cfo\b', r'resign',
                r'appointment', r'restructur', r'bankruptcy', r'dividend',
                r'buyback', r'stock repurchase'
            ],
            EventType.CRYPTO_REGULATION: [
                r'crypto', r'bitcoin', r'ethereum', r'cryptocurrency', r'blockchain',
                r'sec.*crypto', r'crypto.*regulation', r'stablecoin', r'defi'
            ],
            EventType.TECHNOLOGY: [
                r'ai\b', r'artificial intelligence', r'breakthrough', r'innovation',
                r'chip shortage', r'semiconductor', r'data breach', r'cyber'
            ],
            EventType.CRISIS: [
                r'crash', r'collapse', r'panic', r'crisis', r'black swan',
                r'lehman', r'bailout', r'emergency', r'contagion'
            ]
        }
        
        # Compile regex patterns for efficiency
        self.compiled_patterns = {
            event_type: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
            for event_type, patterns in self.event_patterns.items()
        }
        
        # Severity keywords (boost impact score)
        self.severity_keywords = {
            'extreme': 1.0,
            'unprecedented': 0.9,
            'historic': 0.8,
            'major': 0.7,
            'significant': 0.6,
            'severe': 0.7,
            'massive': 0.8,
            'surprise': 0.6,
            'unexpected': 0.5,
            'dramatic': 0.6,
            'sharp': 0.5,
            'substantial': 0.5
        }
        
        # Sector impact mapping (event type -> affected sectors)
        self.sector_impact_map = {
            EventType.MONETARY_POLICY: {
                'primary': ['financials', 'real_estate', 'utilities'],
                'secondary': ['consumer_discretionary', 'materials']
            },
            EventType.GEOPOLITICAL: {
                'primary': ['energy', 'defense', 'industrials'],
                'secondary': ['materials', 'consumer_discretionary']
            },
            EventType.REGULATORY: {
                'primary': ['financials', 'healthcare', 'technology'],
                'secondary': []
            },
            EventType.ECONOMIC_DATA: {
                'primary': ['financials', 'consumer_discretionary', 'industrials'],
                'secondary': ['materials', 'energy']
            },
            EventType.CRYPTO_REGULATION: {
                'primary': ['technology', 'financials'],
                'secondary': []
            },
            EventType.TECHNOLOGY: {
                'primary': ['technology', 'communication_services'],
                'secondary': ['consumer_discretionary']
            },
            EventType.CRISIS: {
                'primary': ['all'],  # Everything affected
                'secondary': []
            }
        }
    
    def analyze_event(
        self,
        event: Dict[str, str],
        historical_events: Optional[List[Dict]] = None
    ) -> Dict:
        """
        Analyze an event and predict its market impact.
        
        Args:
            event: Dict with 'title', 'description' (optional), 'timestamp' (optional)
            historical_events: Optional list of similar past events for pattern matching
        
        Returns:
            Impact analysis with:
                - event_type: EventType classification
                - impact_score: Severity (0-1)
                - affected_sectors: List of impacted sectors
                - affected_assets: List of ticker symbols (if identifiable)
                - timeframe: Expected impact duration
                - volatility_spike_expected: Boolean
                - confidence: Analysis confidence (0-1)
                - reasoning: Explanation of analysis
        """
        # Combine title and description
        text = event.get('title', '')
        description = event.get('description', '')
        if description:
            text = f"{text}. {description}"
        
        # 1. Classify event type
        event_type = self._classify_event_type(text)
        
        # 2. Calculate base impact score
        impact_score = self._calculate_impact_score(text, event_type)
        
        # 3. Identify affected sectors and assets
        affected_sectors = self._identify_affected_sectors(text, event_type)
        affected_assets = self._identify_affected_assets(text)
        
        # 4. Estimate impact timeframe
        timeframe = self._estimate_timeframe(text, event_type)
        
        # 5. Predict volatility spike
        volatility_spike = self._predict_volatility_spike(impact_score, event_type)
        
        # 6. Generate reasoning
        reasoning = self._generate_reasoning(
            event_type, impact_score, affected_sectors, timeframe
        )
        
        # 7. Calculate confidence
        confidence = self._calculate_confidence(text, event_type)
        
        result = {
            'event_type': event_type.value,
            'impact_score': round(impact_score, 3),
            'affected_sectors': affected_sectors,
            'affected_assets': affected_assets,
            'timeframe': timeframe.value,
            'volatility_spike_expected': volatility_spike,
            'confidence': round(confidence, 3),
            'reasoning': reasoning,
            'timestamp': event.get('timestamp', None)
        }
        
        return result
    
    def _classify_event_type(self, text: str) -> EventType:
        """
        Classify event type based on keyword matching.
        
        Args:
            text: Event text
        
        Returns:
            EventType classification
        """
        # Count pattern matches per event type
        match_counts = {}
        
        for event_type, patterns in self.compiled_patterns.items():
            count = sum(1 for pattern in patterns if pattern.search(text))
            match_counts[event_type] = count
        
        # Return type with most matches
        if not match_counts or max(match_counts.values()) == 0:
            return EventType.UNKNOWN
        
        # Find event type with highest count
        max_count = max(match_counts.values())
        for event_type, count in match_counts.items():
            if count == max_count:
                return event_type
        
        return EventType.UNKNOWN
    
    def _calculate_impact_score(self, text: str, event_type: EventType) -> float:
        """
        Calculate impact severity score (0-1).
        
        Args:
            text: Event text
            event_type: Classified event type
        
        Returns:
            Impact score between 0 and 1
        """
        # Base score by event type
        base_scores = {
            EventType.MONETARY_POLICY: 0.7,
            EventType.GEOPOLITICAL: 0.6,
            EventType.REGULATORY: 0.5,
            EventType.EARNINGS: 0.4,
            EventType.ECONOMIC_DATA: 0.5,
            EventType.MARKET_STRUCTURE: 0.6,
            EventType.CORPORATE: 0.3,
            EventType.CRYPTO_REGULATION: 0.4,
            EventType.TECHNOLOGY: 0.3,
            EventType.CRISIS: 0.9,
            EventType.UNKNOWN: 0.2
        }
        
        score = base_scores.get(event_type, 0.3)
        
        # Boost score based on severity keywords
        text_lower = text.lower()
        for keyword, boost in self.severity_keywords.items():
            if keyword in text_lower:
                score = min(1.0, score + boost * 0.2)  # Cap at 1.0
        
        # Check for specific high-impact phrases
        high_impact_phrases = [
            '75 basis points', '100 basis points', 'emergency meeting',
            'black monday', 'market crash', 'circuit breaker triggered',
            'trading halted', 'record high', 'record low', 'all-time high'
        ]
        
        for phrase in high_impact_phrases:
            if phrase in text_lower:
                score = min(1.0, score + 0.15)
        
        return score
    
    def _identify_affected_sectors(
        self,
        text: str,
        event_type: EventType
    ) -> List[str]:
        """
        Identify which market sectors are affected.
        
        Args:
            text: Event text
            event_type: Event classification
        
        Returns:
            List of affected sector names
        """
        # Get default sectors for this event type
        sector_map = self.sector_impact_map.get(event_type, {'primary': [], 'secondary': []})
        affected = sector_map['primary'].copy()
        
        # Add secondary sectors if high impact
        text_lower = text.lower()
        if any(keyword in text_lower for keyword in ['major', 'significant', 'widespread']):
            affected.extend(sector_map['secondary'])
        
        # Check for sector-specific mentions in text
        sector_keywords = {
            'technology': ['tech', 'software', 'chip', 'semiconductor', 'ai', 'cloud'],
            'financials': ['bank', 'finance', 'credit', 'lending', 'mortgage'],
            'healthcare': ['health', 'pharma', 'drug', 'medical', 'biotech'],
            'energy': ['oil', 'gas', 'energy', 'petroleum', 'renewable'],
            'consumer_discretionary': ['retail', 'consumer', 'shopping', 'e-commerce'],
            'industrials': ['manufacturing', 'industrial', 'factory', 'supply chain'],
            'utilities': ['utility', 'electric', 'power', 'water'],
            'materials': ['commodity', 'metal', 'mining', 'steel', 'copper'],
            'real_estate': ['real estate', 'property', 'housing', 'reit'],
            'communication_services': ['telecom', 'media', 'communication'],
            'defense': ['defense', 'military', 'weapon', 'aerospace']
        }
        
        for sector, keywords in sector_keywords.items():
            if any(keyword in text_lower for keyword in keywords):
                if sector not in affected:
                    affected.append(sector)
        
        return list(set(affected))  # Remove duplicates
    
    def _identify_affected_assets(self, text: str) -> List[str]:
        """
        Extract specific ticker symbols or company names mentioned.
        
        Args:
            text: Event text
        
        Returns:
            List of ticker symbols
        """
        # Common ticker patterns (all caps, 1-5 letters)
        ticker_pattern = r'\b([A-Z]{1,5})\b'
        
        # Company name to ticker mapping (subset - expand as needed)
        company_tickers = {
            'apple': 'AAPL',
            'tesla': 'TSLA',
            'nvidia': 'NVDA',
            'microsoft': 'MSFT',
            'amazon': 'AMZN',
            'google': 'GOOGL',
            'meta': 'META',
            'netflix': 'NFLX',
            'jpmorgan': 'JPM',
            'goldman': 'GS',
            'boeing': 'BA',
            'exxon': 'XOM'
        }
        
        tickers = []
        text_lower = text.lower()
        
        # Check for company names
        for company, ticker in company_tickers.items():
            if company in text_lower:
                tickers.append(ticker)
        
        # Extract potential tickers (basic - would need validation against real ticker list)
        matches = re.findall(ticker_pattern, text)
        
        # Filter out common words that aren't tickers
        common_words = {
            'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL',
            'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'DAY', 'GET',
            'HAS', 'HIM', 'HOW', 'ITS', 'MAY', 'NEW', 'NOW', 'OLD',
            'SEE', 'TWO', 'WAY', 'WHO', 'BOY', 'DID', 'ITS', 'LET',
            'PUT', 'SAY', 'SHE', 'TOO', 'USE', 'FED', 'SEC', 'GDP',
            'CEO', 'CFO', 'IPO', 'ETF', 'QE', 'AI', 'US', 'UK', 'EU'
        }
        
        for match in matches:
            if match not in common_words and len(match) <= 5:
                tickers.append(match)
        
        return list(set(tickers))  # Remove duplicates
    
    def _estimate_timeframe(
        self,
        text: str,
        event_type: EventType
    ) -> ImpactTimeframe:
        """
        Estimate how long the event impact will last.
        
        Args:
            text: Event text
            event_type: Event classification
        
        Returns:
            ImpactTimeframe enum
        """
        # Default timeframes by event type
        default_timeframes = {
            EventType.MONETARY_POLICY: ImpactTimeframe.LONG,
            EventType.GEOPOLITICAL: ImpactTimeframe.MEDIUM,
            EventType.REGULATORY: ImpactTimeframe.LONG,
            EventType.EARNINGS: ImpactTimeframe.SHORT,
            EventType.ECONOMIC_DATA: ImpactTimeframe.SHORT,
            EventType.MARKET_STRUCTURE: ImpactTimeframe.IMMEDIATE,
            EventType.CORPORATE: ImpactTimeframe.MEDIUM,
            EventType.CRYPTO_REGULATION: ImpactTimeframe.MEDIUM,
            EventType.TECHNOLOGY: ImpactTimeframe.LONG,
            EventType.CRISIS: ImpactTimeframe.STRUCTURAL,
            EventType.UNKNOWN: ImpactTimeframe.SHORT
        }
        
        timeframe = default_timeframes.get(event_type, ImpactTimeframe.SHORT)
        
        # Adjust based on keywords
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['permanent', 'structural', 'fundamental']):
            timeframe = ImpactTimeframe.STRUCTURAL
        elif any(word in text_lower for word in ['long-term', 'sustained', 'prolonged']):
            timeframe = ImpactTimeframe.LONG
        elif any(word in text_lower for word in ['immediate', 'instant', 'sudden']):
            timeframe = ImpactTimeframe.IMMEDIATE
        
        return timeframe
    
    def _predict_volatility_spike(
        self,
        impact_score: float,
        event_type: EventType
    ) -> bool:
        """
        Predict if event will cause volatility spike.
        
        Args:
            impact_score: Event impact severity
            event_type: Event classification
        
        Returns:
            True if volatility spike expected
        """
        # High-volatility event types
        high_vol_types = {
            EventType.MONETARY_POLICY,
            EventType.CRISIS,
            EventType.MARKET_STRUCTURE,
            EventType.GEOPOLITICAL
        }
        
        # Threshold: high impact OR high-vol event type
        return impact_score > 0.6 or event_type in high_vol_types
    
    def _generate_reasoning(
        self,
        event_type: EventType,
        impact_score: float,
        affected_sectors: List[str],
        timeframe: ImpactTimeframe
    ) -> str:
        """Generate human-readable reasoning for the analysis."""
        
        reasoning_parts = []
        
        # Event type
        reasoning_parts.append(f"Classified as {event_type.value.replace('_', ' ')} event.")
        
        # Impact severity
        if impact_score > 0.8:
            reasoning_parts.append("High impact severity detected.")
        elif impact_score > 0.5:
            reasoning_parts.append("Moderate impact severity.")
        else:
            reasoning_parts.append("Low to moderate impact.")
        
        # Affected sectors
        if affected_sectors:
            sectors_str = ", ".join(affected_sectors[:3])
            reasoning_parts.append(f"Primary sectors affected: {sectors_str}.")
        
        # Timeframe
        reasoning_parts.append(f"Expected impact timeframe: {timeframe.value}.")
        
        return " ".join(reasoning_parts)
    
    def _calculate_confidence(self, text: str, event_type: EventType) -> float:
        """
        Calculate confidence in the analysis.
        
        Args:
            text: Event text
            event_type: Classified event type
        
        Returns:
            Confidence score (0-1)
        """
        confidence = 0.5  # Base confidence
        
        # Boost if strong pattern matches
        if event_type != EventType.UNKNOWN:
            confidence += 0.2
        
        # Boost if text is substantial
        if len(text.split()) > 20:
            confidence += 0.1
        
        # Boost if specific numbers/details present
        if any(char.isdigit() for char in text):
            confidence += 0.1
        
        # Boost if has source/attribution
        if any(source in text.lower() for source in ['reuters', 'bloomberg', 'cnbc', 'wsj']):
            confidence += 0.1
        
        return min(1.0, confidence)


if __name__ == '__main__':
    # Test Event Impact Analyzer
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 70)
    print("Testing Event Impact Analyzer")
    print("=" * 70)
    
    analyzer = EventImpactAnalyzer()
    
    # Test events
    test_events = [
        {
            'title': 'Fed raises interest rates by 75 basis points in aggressive move',
            'description': 'Federal Reserve announces largest rate hike in decades to combat inflation',
            'timestamp': '2024-01-15 14:00:00'
        },
        {
            'title': 'US imposes new tariffs on Chinese imports',
            'description': 'Trade tensions escalate as administration announces 25% tariffs on tech goods',
            'timestamp': '2024-02-01 09:00:00'
        },
        {
            'title': 'SEC announces major cryptocurrency regulation framework',
            'description': 'New rules aim to bring crypto exchanges under federal oversight',
            'timestamp': '2024-03-10 10:30:00'
        },
        {
            'title': 'NVIDIA reports record earnings, beats estimates significantly',
            'description': 'AI chip demand drives 200% revenue growth year-over-year',
            'timestamp': '2024-04-05 16:00:00'
        },
        {
            'title': 'Circuit breakers triggered as market plunges 7%',
            'description': 'Trading halted amid panic selling, worst day since 2020',
            'timestamp': '2024-05-12 10:15:00'
        }
    ]
    
    print("\nAnalyzing Events:")
    print("=" * 70)
    
    for i, event in enumerate(test_events, 1):
        print(f"\n[{i}] {event['title']}")
        print("-" * 70)
        
        result = analyzer.analyze_event(event)
        
        print(f"Event Type: {result['event_type'].upper()}")
        print(f"Impact Score: {result['impact_score']:.2f} / 1.00")
        print(f"Timeframe: {result['timeframe']}")
        print(f"Volatility Spike: {'YES' if result['volatility_spike_expected'] else 'NO'}")
        print(f"Affected Sectors: {', '.join(result['affected_sectors'])}")
        if result['affected_assets']:
            print(f"Affected Assets: {', '.join(result['affected_assets'])}")
        print(f"Confidence: {result['confidence']:.2f}")
        print(f"Reasoning: {result['reasoning']}")
    
    print("\n" + "=" * 70)
    print("✓ Event Impact Analyzer test completed!")
    print("=" * 70)
