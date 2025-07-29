import asyncio
import aiohttp
import feedparser
import pandas as pd
from datetime import datetime, timedelta
import json
import logging
from typing import List, Dict, Optional
import hashlib
import re
from dataclasses import dataclass
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark.sql.types import *
import sqlite3
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class NewsArticle:
    """Structured news article data"""
    id: str
    title: str
    content: str
    source: str
    published_at: datetime
    url: str
    symbols: List[str]
    sentiment_score: Optional[float] = None
    category: Optional[str] = None

class FreeNewsCollector:
    """
    Collects financial news from free sources:
    - RSS Feeds (Yahoo Finance, MarketWatch, etc.)
    - Reddit API (r/stocks, r/investing, etc.)
    - NewsAPI (free tier)
    """
    
    def __init__(self):
        self.session = None
        self.spark = None
        self.articles_cache = set()
        
        # Free RSS feeds for financial news
        self.rss_feeds = [
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
            "https://www.marketwatch.com/rss/topstories",
            "https://seekingalpha.com/market_currents.xml",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "https://www.reuters.com/rssFeed/businessNews",
            "https://feeds.bloomberg.com/markets/news.rss"
        ]
        
        # Stock symbols to track
        self.tracked_symbols = [
            'AAPL', 'GOOGL', 'MSFT', 'AMZN', 'TSLA', 'META', 'NFLX',
            'NVDA', 'AMD', 'INTC', 'JPM', 'BAC', 'WFC', 'GS', 'C',
            'SPY', 'QQQ', 'IWM', 'DIA'  # ETFs
        ]
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def init_spark(self):
        """Initialize Spark session for large-scale processing"""
        self.spark = SparkSession.builder \
            .appName("TradeSentinel-NewsCollector") \
            .config("spark.sql.adaptive.enabled", "true") \
            .config("spark.sql.adaptive.coalescePartitions.enabled", "true") \
            .getOrCreate()
        
        logger.info("Spark session initialized")
    
    def generate_article_id(self, title: str, source: str, published_at: datetime) -> str:
        """Generate unique ID for article"""
        content = f"{title}_{source}_{published_at.isoformat()}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def extract_stock_symbols(self, text: str) -> List[str]:
        """Extract stock symbols from text using regex"""
        # Common stock symbol patterns
        patterns = [
            r'\$([A-Z]{1,5})',  # $AAPL format
            r'\b([A-Z]{2,5})\b',  # AAPL format (2-5 letters)
        ]
        
        symbols = set()
        for pattern in patterns:
            matches = re.findall(pattern, text.upper())
            for match in matches:
                if match in self.tracked_symbols:
                    symbols.add(match)
        
        return list(symbols)
    
    async def collect_rss_feeds(self) -> List[NewsArticle]:
        """Collect news from RSS feeds"""
        articles = []
        
        for feed_url in self.rss_feeds:
            try:
                logger.info(f"Fetching RSS feed: {feed_url}")
                
                async with self.session.get(feed_url, timeout=30) as response:
                    if response.status == 200:
                        content = await response.text()
                        feed = feedparser.parse(content)
                        
                        for entry in feed.entries:
                            # Parse publication date
                            pub_date = datetime.now()
                            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                                pub_date = datetime(*entry.published_parsed[:6])
                            
                            # Extract content
                            content = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
                            title = getattr(entry, 'title', '')
                            
                            # Generate unique ID
                            article_id = self.generate_article_id(
                                title, feed.feed.get('title', 'Unknown'), pub_date
                            )
                            
                            # Skip duplicates
                            if article_id in self.articles_cache:
                                continue
                            
                            # Extract stock symbols
                            symbols = self.extract_stock_symbols(f"{title} {content}")
                            
                            # Only include articles with relevant stock symbols
                            if symbols:
                                article = NewsArticle(
                                    id=article_id,
                                    title=title,
                                    content=content,
                                    source=feed.feed.get('title', 'RSS Feed'),
                                    published_at=pub_date,
                                    url=getattr(entry, 'link', ''),
                                    symbols=symbols
                                )
                                articles.append(article)
                                self.articles_cache.add(article_id)
                        
                        logger.info(f"Collected {len([a for a in articles if feed.feed.get('title', 'RSS') in a.source])} articles from {feed.feed.get('title', 'RSS')}")
                        
            except Exception as e:
                logger.error(f"Error fetching RSS feed {feed_url}: {e}")
                continue
        
        return articles
    
    async def collect_reddit_data(self) -> List[NewsArticle]:
        """Collect posts from financial subreddits (using Reddit JSON API - no auth required)"""
        articles = []
        subreddits = ['stocks', 'investing', 'SecurityAnalysis', 'ValueInvesting', 'StockMarket']
        
        for subreddit in subreddits:
            try:
                url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=50"
                headers = {'User-Agent': 'TradeSentinel/1.0'}
                
                async with self.session.get(url, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        for post in data['data']['children']:
                            post_data = post['data']
                            
                            # Skip removed/deleted posts
                            if post_data.get('removed_by_category') or not post_data.get('title'):
                                continue
                            
                            # Parse timestamp
                            pub_date = datetime.fromtimestamp(post_data['created_utc'])
                            
                            # Generate ID
                            article_id = self.generate_article_id(
                                post_data['title'], f"r/{subreddit}", pub_date
                            )
                            
                            # Skip duplicates
                            if article_id in self.articles_cache:
                                continue
                            
                            # Extract content
                            content = post_data.get('selftext', '') or post_data.get('title', '')
                            title = post_data['title']
                            
                            # Extract stock symbols
                            symbols = self.extract_stock_symbols(f"{title} {content}")
                            
                            # Only include posts with relevant stock symbols and good engagement
                            if symbols and post_data.get('score', 0) > 10:
                                article = NewsArticle(
                                    id=article_id,
                                    title=title,
                                    content=content,
                                    source=f"r/{subreddit}",
                                    published_at=pub_date,
                                    url=f"https://reddit.com{post_data['permalink']}",
                                    symbols=symbols,
                                    category='social_media'
                                )
                                articles.append(article)
                                self.articles_cache.add(article_id)
                
                logger.info(f"Collected articles from r/{subreddit}")
                
            except Exception as e:
                logger.error(f"Error fetching Reddit data from r/{subreddit}: {e}")
                continue
        
        return articles
    
    def save_to_database(self, articles: List[NewsArticle], db_path: str = "news_data.db"):
        """Save articles to SQLite database"""
        conn = sqlite3.connect(db_path)
        
        # Create table if not exists
        conn.execute('''
            CREATE TABLE IF NOT EXISTS news_articles (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                source TEXT,
                published_at TIMESTAMP,
                url TEXT,
                symbols TEXT,
                sentiment_score REAL,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Insert articles
        for article in articles:
            try:
                conn.execute('''
                    INSERT OR REPLACE INTO news_articles 
                    (id, title, content, source, published_at, url, symbols, sentiment_score, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    article.id,
                    article.title,
                    article.content,
                    article.source,
                    article.published_at,
                    article.url,
                    json.dumps(article.symbols),
                    article.sentiment_score,
                    article.category
                ))
            except Exception as e:
                logger.error(f"Error inserting article {article.id}: {e}")
        
        conn.commit()
        conn.close()
        logger.info(f"Saved {len(articles)} articles to database")
    
    def process_with_spark(self, db_path: str = "news_data.db"):
        """Process news data using Spark for large-scale analysis"""
        if not self.spark:
            self.init_spark()
        
        # Read data from SQLite
        df = self.spark.read \
            .format("jdbc") \
            .option("url", f"jdbc:sqlite:{db_path}") \
            .option("dbtable", "news_articles") \
            .option("driver", "org.sqlite.JDBC") \
            .load()
        
        # Data quality checks
        df_clean = df.filter(
            (col("title").isNotNull()) & 
            (col("content").isNotNull()) &
            (length(col("title")) > 10)
        )
        
        # Add processing timestamp
        df_processed = df_clean.withColumn("processed_at", current_timestamp())
        
        # Calculate article statistics by source
        source_stats = df_processed.groupBy("source") \
            .agg(
                count("*").alias("article_count"),
                countDistinct("symbols").alias("unique_symbols"),
                avg(length("content")).alias("avg_content_length")
            )
        
        logger.info("Source Statistics:")
        source_stats.show()
        
        # Symbol frequency analysis
        symbols_df = df_processed.select(
            col("id"),
            explode(from_json(col("symbols"), ArrayType(StringType()))).alias("symbol")
        )
        
        symbol_stats = symbols_df.groupBy("symbol") \
            .agg(count("*").alias("mention_count")) \
            .orderBy(desc("mention_count"))
        
        logger.info("Top mentioned symbols:")
        symbol_stats.show(20)
        
        return df_processed
    
    async def run_collection_cycle(self):
        """Run one complete news collection cycle"""
        logger.info("Starting news collection cycle...")
        
        all_articles = []
        
        # Collect from RSS feeds
        rss_articles = await self.collect_rss_feeds()
        all_articles.extend(rss_articles)
        
        # Collect from Reddit
        reddit_articles = await self.collect_reddit_data()
        all_articles.extend(reddit_articles)
        
        logger.info(f"Total articles collected: {len(all_articles)}")
        
        # Save to database
        if all_articles:
            self.save_to_database(all_articles)
            
            # Process with Spark
            self.process_with_spark()
        
        return all_articles

# Main execution
async def main():
    """Main function to run the news collector"""
    async with FreeNewsCollector() as collector:
        articles = await collector.run_collection_cycle()
        
        print(f"\n🎉 Successfully collected {len(articles)} news articles!")
        print("📊 Sample articles:")
        
        for i, article in enumerate(articles[:3]):
            print(f"\n{i+1}. {article.title[:100]}...")
            print(f"   Source: {article.source}")
            print(f"   Symbols: {', '.join(article.symbols)}")
            print(f"   Published: {article.published_at}")

if __name__ == "__main__":
    # Install required packages first:
    # pip install aiohttp feedparser pandas pyspark sqlite3
    asyncio.run(main())