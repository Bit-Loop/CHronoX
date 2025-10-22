"""
Data Ingestion DAG

Orchestrates real-time and batch data ingestion from multiple sources.
Runs every 5 minutes to fetch latest market data.
"""

from datetime import datetime, timedelta
from typing import Dict, Any

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.utils.task_group import TaskGroup
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sensors.external_task import ExternalTaskSensor

import logging

logger = logging.getLogger(__name__)


default_args = {
    'owner': 'chronox',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email': ['alerts@chronox.ai'],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=2),
    'retry_exponential_backoff': True,
    'max_retry_delay': timedelta(minutes=10),
}


def fetch_market_data(**context) -> Dict[str, Any]:
    """
    Fetch real-time market data from multiple sources.
    
    Returns:
        Dictionary with ingestion statistics
    """
    from data_pipeline.market_data_fetcher import MarketDataFetcher
    from data_pipeline.config import SYMBOLS, EXCHANGES
    
    execution_date = context['execution_date']
    logger.info(f"Fetching market data for {execution_date}")
    
    fetcher = MarketDataFetcher()
    stats = {
        'total_symbols': 0,
        'successful': 0,
        'failed': 0,
        'records_ingested': 0
    }
    
    for exchange in EXCHANGES:
        for symbol in SYMBOLS:
            try:
                data = fetcher.fetch_realtime(
                    symbol=symbol,
                    exchange=exchange,
                    execution_date=execution_date
                )
                
                if data is not None and len(data) > 0:
                    stats['successful'] += 1
                    stats['records_ingested'] += len(data)
                else:
                    stats['failed'] += 1
                    
                stats['total_symbols'] += 1
                
            except Exception as e:
                logger.error(f"Failed to fetch {symbol} from {exchange}: {e}")
                stats['failed'] += 1
    
    # Push stats to XCom for monitoring
    context['task_instance'].xcom_push(key='ingestion_stats', value=stats)
    
    logger.info(f"Ingestion complete: {stats}")
    return stats


def process_alternative_data(**context) -> Dict[str, Any]:
    """
    Process alternative data sources (news, social media, etc.)
    
    Returns:
        Dictionary with processing statistics
    """
    from data_pipeline.alternative_data_processor import AlternativeDataProcessor
    
    execution_date = context['execution_date']
    logger.info(f"Processing alternative data for {execution_date}")
    
    processor = AlternativeDataProcessor()
    
    # Fetch news data
    news_stats = processor.fetch_news(execution_date)
    
    # Fetch social media sentiment
    social_stats = processor.fetch_social_sentiment(execution_date)
    
    # Fetch on-chain data
    onchain_stats = processor.fetch_onchain_metrics(execution_date)
    
    stats = {
        'news_articles': news_stats.get('count', 0),
        'social_posts': social_stats.get('count', 0),
        'onchain_metrics': onchain_stats.get('count', 0),
        'total_records': sum([
            news_stats.get('count', 0),
            social_stats.get('count', 0),
            onchain_stats.get('count', 0)
        ])
    }
    
    context['task_instance'].xcom_push(key='alternative_stats', value=stats)
    
    logger.info(f"Alternative data processing complete: {stats}")
    return stats


def validate_data_quality(**context) -> bool:
    """
    Validate ingested data quality.
    
    Returns:
        True if validation passes
        
    Raises:
        ValueError if critical quality issues detected
    """
    from data_pipeline.data_validator import DataValidator
    
    execution_date = context['execution_date']
    logger.info(f"Validating data quality for {execution_date}")
    
    validator = DataValidator()
    
    # Get ingestion stats from XCom
    ingestion_stats = context['task_instance'].xcom_pull(
        key='ingestion_stats',
        task_ids='fetch_market_data'
    )
    
    # Validate completeness
    completeness_score = validator.check_completeness(execution_date)
    if completeness_score < 0.95:
        logger.warning(f"Low data completeness: {completeness_score:.2%}")
    
    # Validate accuracy
    accuracy_issues = validator.check_accuracy(execution_date)
    if len(accuracy_issues) > 0:
        logger.warning(f"Found {len(accuracy_issues)} accuracy issues")
    
    # Validate timeliness
    latency = validator.check_timeliness(execution_date)
    if latency > 300:  # 5 minutes
        logger.warning(f"High data latency: {latency}s")
    
    # Critical check: fail if too many symbols failed
    failure_rate = ingestion_stats['failed'] / max(ingestion_stats['total_symbols'], 1)
    if failure_rate > 0.2:
        raise ValueError(f"High failure rate: {failure_rate:.2%}")
    
    validation_results = {
        'completeness': completeness_score,
        'accuracy_issues': len(accuracy_issues),
        'latency_seconds': latency,
        'passed': True
    }
    
    context['task_instance'].xcom_push(key='validation_results', value=validation_results)
    
    logger.info(f"Data quality validation passed: {validation_results}")
    return True


def store_to_influxdb(**context) -> int:
    """
    Store validated data to InfluxDB.
    
    Returns:
        Number of records written
    """
    from data_pipeline.influxdb_writer import InfluxDBWriter
    
    execution_date = context['execution_date']
    logger.info(f"Writing data to InfluxDB for {execution_date}")
    
    writer = InfluxDBWriter()
    
    # Write market data
    market_records = writer.write_market_data(execution_date)
    
    # Write alternative data
    alt_records = writer.write_alternative_data(execution_date)
    
    total_records = market_records + alt_records
    
    logger.info(f"Wrote {total_records} records to InfluxDB")
    return total_records


def update_metadata(**context) -> None:
    """
    Update PostgreSQL metadata tables.
    """
    from data_pipeline.metadata_updater import MetadataUpdater
    
    execution_date = context['execution_date']
    logger.info(f"Updating metadata for {execution_date}")
    
    updater = MetadataUpdater()
    
    # Get all stats from XCom
    ingestion_stats = context['task_instance'].xcom_pull(
        key='ingestion_stats',
        task_ids='fetch_market_data'
    )
    
    validation_results = context['task_instance'].xcom_pull(
        key='validation_results',
        task_ids='validate_data_quality'
    )
    
    # Update ingestion log
    updater.log_ingestion_run(
        execution_date=execution_date,
        stats=ingestion_stats,
        validation=validation_results
    )
    
    # Update data catalog
    updater.update_data_catalog(execution_date)
    
    logger.info("Metadata update complete")


def check_data_freshness(**context) -> bool:
    """
    Check if we have fresh data for training/inference.
    
    Returns:
        True if data is fresh enough
    """
    from data_pipeline.freshness_checker import FreshnessChecker
    
    checker = FreshnessChecker()
    is_fresh = checker.check_freshness(max_age_minutes=10)
    
    if not is_fresh:
        logger.warning("Data is stale, may impact model performance")
    
    return is_fresh


# Define DAG
with DAG(
    dag_id='data_ingestion',
    default_args=default_args,
    description='Ingest and validate market data from multiple sources',
    schedule_interval='*/5 * * * *',  # Every 5 minutes
    catchup=False,
    max_active_runs=1,
    tags=['data', 'ingestion', 'real-time'],
) as dag:
    
    # Start marker
    start = BashOperator(
        task_id='start',
        bash_command='echo "Starting data ingestion pipeline"'
    )
    
    # Fetch market data
    fetch_data = PythonOperator(
        task_id='fetch_market_data',
        python_callable=fetch_market_data,
        provide_context=True,
    )
    
    # Process alternative data in parallel
    process_alt = PythonOperator(
        task_id='process_alternative_data',
        python_callable=process_alternative_data,
        provide_context=True,
    )
    
    # Validate data quality
    validate = PythonOperator(
        task_id='validate_data_quality',
        python_callable=validate_data_quality,
        provide_context=True,
    )
    
    # Store to InfluxDB
    store_influx = PythonOperator(
        task_id='store_to_influxdb',
        python_callable=store_to_influxdb,
        provide_context=True,
    )
    
    # Update metadata
    update_meta = PythonOperator(
        task_id='update_metadata',
        python_callable=update_metadata,
        provide_context=True,
    )
    
    # Check freshness
    check_fresh = PythonOperator(
        task_id='check_data_freshness',
        python_callable=check_data_freshness,
        provide_context=True,
    )
    
    # End marker
    end = BashOperator(
        task_id='end',
        bash_command='echo "Data ingestion pipeline complete"'
    )
    
    # Define task dependencies
    start >> [fetch_data, process_alt]
    [fetch_data, process_alt] >> validate
    validate >> store_influx
    store_influx >> update_meta
    update_meta >> check_fresh
    check_fresh >> end
