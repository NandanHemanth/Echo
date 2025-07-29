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
import os
import sqlite3

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class NewsArticle:
    """Structured data for news articles"""
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

        
