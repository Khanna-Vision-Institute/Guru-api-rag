import json
import re
from typing import Optional, Tuple
from difflib import SequenceMatcher

def load_faqs(faq_file='guru_faqs.json'):
    """Load FAQ knowledge base from JSON file"""
    try:
        with open(faq_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading FAQs: {e}")
        return []

def calculate_similarity(query: str, text: str) -> float:
    """Calculate similarity between query and text using SequenceMatcher"""
    return SequenceMatcher(None, query.lower(), text.lower()).ratio()

def match_faq(query: str, threshold: float = 0.5) -> Optional[Tuple[str, float]]:
    """
    Match user query against FAQ knowledge base
    Returns (answer, confidence) tuple if match found, None otherwise
    """
    faqs = load_faqs()
    if not faqs:
        return None
    
    query_lower = query.lower().strip()
    best_match = None
    best_score = 0.0
    
    for faq in faqs:
        question = faq.get('question', '')
        answer = faq.get('answer', '')
        keywords = faq.get('keywords', [])
        
        # Calculate similarity with question
        question_score = calculate_similarity(query_lower, question.lower())
        
        # Check keyword matches (boost score if keywords found)
        keyword_boost = 0.0
        for keyword in keywords:
            if keyword.lower() in query_lower:
                keyword_boost += 0.15
        
        # Total score (capped at 1.0)
        total_score = min(question_score + keyword_boost, 1.0)
        
        if total_score > best_score:
            best_score = total_score
            best_match = answer
    
    # Return match if confidence is above threshold
    if best_score >= threshold:
        return (best_match, best_score)
    
    return None
