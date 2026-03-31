"""
price_predictor.py
------------------
Predicts expected price ranges for shipments using historical data + AI analysis.
"""
import logging
import math
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)
client = OpenAI()


class PricePrediction(BaseModel):
    predicted_low: float = Field(description="Lower bound of expected price range")
    predicted_high: float = Field(description="Upper bound of expected price range")
    confidence: str = Field(description="Confidence level: high, medium, or low")
    explanation: str = Field(description="Brief explanation of the prediction")


def predict_price(shipment: dict, history_matches: list) -> PricePrediction:
    """Predict the expected price range for a shipment.

    Uses historical match data to compute a weighted average and range.
    Falls back to AI estimation if no history is available.

    Args:
        shipment: dict with origin, destination, mode, weight_kg, commodity
        history_matches: list of dicts from history_agent with rate_paid,
                        transit_time_days, similarity, weight_kg
    """
    if not history_matches:
        return _ai_estimate(shipment)

    # Compute weighted average using similarity scores as weights
    rates = []
    weights = []
    for m in history_matches:
        rate = m.get("rate_paid")
        sim = m.get("similarity", 0.5)
        if rate and rate > 0:
            rates.append(float(rate))
            weights.append(float(sim))

    if not rates:
        return _ai_estimate(shipment)

    # Weighted average
    total_weight = sum(weights)
    weighted_avg = sum(r * w for r, w in zip(rates, weights)) / total_weight

    # Standard deviation
    if len(rates) >= 2:
        variance = sum(w * (r - weighted_avg) ** 2 for r, w in zip(rates, weights)) / total_weight
        stdev = math.sqrt(variance)
    else:
        stdev = weighted_avg * 0.15  # 15% margin for single match

    predicted_low = max(0, weighted_avg - stdev)
    predicted_high = weighted_avg + stdev

    # Confidence based on number of matches and similarity
    avg_sim = sum(weights) / len(weights)
    if len(rates) >= 3 and avg_sim > 0.7:
        confidence = "high"
    elif len(rates) >= 2 and avg_sim > 0.5:
        confidence = "medium"
    else:
        confidence = "low"

    explanation = (
        f"Based on {len(rates)} historical shipment(s) with average similarity {avg_sim:.2f}. "
        f"Weighted average rate: ${weighted_avg:.0f}."
    )

    return PricePrediction(
        predicted_low=round(predicted_low, 2),
        predicted_high=round(predicted_high, 2),
        confidence=confidence,
        explanation=explanation,
    )


def _ai_estimate(shipment: dict) -> PricePrediction:
    """Fallback: use AI to estimate price when no history exists."""
    prompt = (
        f"Estimate a reasonable freight rate range for this shipment:\n"
        f"Origin: {shipment.get('origin', 'Unknown')}\n"
        f"Destination: {shipment.get('destination', 'Unknown')}\n"
        f"Mode: {shipment.get('mode', 'Unknown')}\n"
        f"Weight: {shipment.get('weight_kg', 'Unknown')} kg\n"
        f"Commodity: {shipment.get('commodity', 'Unknown')}\n\n"
        f"Provide a low and high estimate in USD."
    )

    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "You are a freight rate estimation expert. Provide realistic price "
                "range estimates based on current market conditions. Set confidence to 'low' "
                "since this is an AI estimate without historical data."
            )},
            {"role": "user", "content": prompt}
        ],
        response_format=PricePrediction,
    )
    return completion.choices[0].message.parsed


def assess_quotation(rate: float, prediction: PricePrediction) -> str:
    """Assess whether a quotation rate is within the predicted range.

    Returns: "within_range", "above_expected", or "below_expected"
    """
    if rate < prediction.predicted_low:
        return "below_expected"
    elif rate > prediction.predicted_high:
        return "above_expected"
    return "within_range"


if __name__ == "__main__":
    mock_shipment = {
        "origin": "Hamburg port",
        "destination": "Mumbai",
        "mode": "sea_freight",
        "weight_kg": 1500,
        "commodity": "spare automotive parts"
    }
    mock_history = [
        {"rate_paid": 2100, "transit_time_days": 28, "similarity": 0.85, "weight_kg": 1400},
        {"rate_paid": 1200, "transit_time_days": 30, "similarity": 0.72, "weight_kg": 800},
    ]
    prediction = predict_price(mock_shipment, mock_history)
    print(f"Predicted range: ${prediction.predicted_low:.0f} - ${prediction.predicted_high:.0f}")
    print(f"Confidence: {prediction.confidence}")
    print(f"Explanation: {prediction.explanation}")

    print(f"\nAssessment of $2000: {assess_quotation(2000, prediction)}")
    print(f"Assessment of $3500: {assess_quotation(3500, prediction)}")
    print(f"Assessment of $800: {assess_quotation(800, prediction)}")
