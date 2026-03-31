"""
train_classifier.py
-------------------
Step 2 of the fine-tuning pipeline.

Uploads a JSONL training file to OpenAI and starts a fine-tuning job.
When the job completes, saves the fine-tuned model ID to your .env file.

Usage:
    # Full pipeline: build data first, then train
    python build_training_data.py --sample 300
    python train_classifier.py --file training_data.jsonl

    # Or just check the status of an existing job
    python train_classifier.py --status ftjob-abc123
"""

import argparse
import os
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv, set_key
from openai import OpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

client = OpenAI()
ENV_FILE = Path(__file__).parent / ".env"
BASE_MODEL = "gpt-4o-mini-2024-07-18"


def upload_training_file(file_path: str) -> str:
    """Upload JSONL file to OpenAI and return file ID."""
    path = Path(file_path)
    if not path.exists():
        logger.error("Training file not found: %s", file_path)
        sys.exit(1)

    logger.info("Uploading %s (%.1f KB)...", path.name, path.stat().st_size / 1024)
    with open(path, "rb") as f:
        response = client.files.create(file=f, purpose="fine-tune")

    logger.info("Uploaded: file ID = %s", response.id)
    return response.id


def start_fine_tuning(file_id: str, suffix: str = "email-classifier") -> str:
    """Create a fine-tuning job and return the job ID."""
    logger.info("Creating fine-tuning job on %s...", BASE_MODEL)

    job = client.fine_tuning.jobs.create(
        training_file=file_id,
        model=BASE_MODEL,
        suffix=suffix,
        method={
            "type": "supervised",
            "supervised": {
                "hyperparameters": {
                    "n_epochs": 3,
                }
            },
        },
    )

    logger.info("Fine-tuning job created: %s (status: %s)", job.id, job.status)
    return job.id


def poll_until_done(job_id: str, poll_interval: int = 30) -> str | None:
    """Poll the fine-tuning job until it completes. Returns fine-tuned model ID or None."""
    logger.info("Polling job %s (every %ds)...", job_id, poll_interval)

    while True:
        job = client.fine_tuning.jobs.retrieve(job_id)
        status = job.status
        logger.info("  Status: %s", status)

        if status == "succeeded":
            model_id = job.fine_tuned_model
            logger.info("✓ Fine-tuning succeeded! Model ID: %s", model_id)
            return model_id

        if status in ("failed", "cancelled"):
            logger.error("Fine-tuning %s. Check OpenAI dashboard for details.", status)
            # Print last few events
            try:
                events = client.fine_tuning.jobs.list_events(fine_tuning_job_id=job_id, limit=5)
                for event in events.data:
                    logger.error("  Event: %s", event.message)
            except Exception:
                pass
            return None

        time.sleep(poll_interval)


def save_model_id_to_env(model_id: str):
    """Save the fine-tuned model ID to .env file."""
    try:
        set_key(str(ENV_FILE), "CLASSIFIER_MODEL_ID", model_id)
        logger.info("Saved CLASSIFIER_MODEL_ID=%s to %s", model_id, ENV_FILE)
    except Exception as e:
        logger.warning("Could not auto-save to .env: %s", e)
        logger.info("Please add this to your .env manually:\n  CLASSIFIER_MODEL_ID=%s", model_id)


def check_status(job_id: str):
    """Print the current status and recent events for a job."""
    try:
        job = client.fine_tuning.jobs.retrieve(job_id)
        print(f"\nJob:    {job.id}")
        print(f"Status: {job.status}")
        print(f"Model:  {job.fine_tuned_model or '(pending)'}")
        print(f"Tokens: {job.trained_tokens or 'N/A'}")

        print("\nRecent events:")
        events = client.fine_tuning.jobs.list_events(fine_tuning_job_id=job_id, limit=10)
        for event in reversed(events.data):
            print(f"  [{event.created_at}] {event.message}")

        if job.status == "succeeded" and job.fine_tuned_model:
            save_model_id_to_env(job.fine_tuned_model)
    except Exception as e:
        logger.error("Failed to retrieve job: %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fine-tune GPT-4o-mini for email classification")
    parser.add_argument("--file", type=str, help="Path to JSONL training file")
    parser.add_argument("--status", type=str, help="Check status of an existing job ID")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for job to complete (just start it)")
    args = parser.parse_args()

    if args.status:
        check_status(args.status)
        sys.exit(0)

    if not args.file:
        logger.error("Provide --file path/to/training_data.jsonl  OR  --status ftjob-xxx")
        sys.exit(1)

    # Count examples
    with open(args.file) as f:
        num_examples = sum(1 for _ in f)
    logger.info("Training file has %d examples", num_examples)

    if num_examples < 10:
        logger.error("OpenAI requires at least 10 training examples. Found %d.", num_examples)
        sys.exit(1)
    if num_examples < 50:
        logger.warning("Only %d examples — recommend 50+ per class for good accuracy.", num_examples)

    # Upload and start training
    file_id = upload_training_file(args.file)
    job_id = start_fine_tuning(file_id)

    if args.no_wait:
        logger.info("Job started. Check status later with:\n  python train_classifier.py --status %s", job_id)
        sys.exit(0)

    # Wait for completion (fine-tuning typically takes 15-60 minutes)
    logger.info("Fine-tuning usually takes 15-60 minutes. You can Ctrl+C and check later with --status.")
    model_id = poll_until_done(job_id)
    if model_id:
        save_model_id_to_env(model_id)
        print(f"\n✓ Your fine-tuned model is ready: {model_id}")
        print(f"  It has been saved to .env as CLASSIFIER_MODEL_ID")
        print(f"  Restart the API server to activate it.")
