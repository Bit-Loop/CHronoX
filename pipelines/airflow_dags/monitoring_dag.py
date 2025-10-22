"""
Monitoring DAG

Monitors system health, model performance, and data quality.
Runs hourly to detect issues and trigger alerts.
"""

from datetime import datetime, timedelta
from typing import Dict, Any

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator

import logging

logger = logging.getLogger(__name__)


default_args = {
    'owner': 'chronox',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email': ['alerts@chronox.ai'],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}


def check_model_drift(**context) -> Dict[str, Any]:
    """
    Check for model drift across all deployed models.
    
    Returns:
        Drift detection results
    """
    from monitoring.drift_detector import DriftDetector
    
    execution_date = context['execution_date']
    logger.info(f"Checking model drift at {execution_date}")
    
    detector = DriftDetector()
    
    drift_results = {}
    
    for model_name in ['transformer', 'lstm', 'xgboost', 'rl']:
        result = detector.detect_drift(
            model_name=model_name,
            lookback_days=7,
            drift_threshold=0.3
        )
        drift_results[model_name] = result
        
        if result['drift_detected']:
            logger.warning(f"Drift detected in {model_name}: {result['drift_score']:.3f}")
    
    context['task_instance'].xcom_push(key='drift_results', value=drift_results)
    
    return drift_results


def check_model_performance(**context) -> Dict[str, Any]:
    """
    Monitor model performance metrics.
    
    Returns:
        Performance metrics for all models
    """
    from monitoring.performance_monitor import PerformanceMonitor
    
    execution_date = context['execution_date']
    logger.info(f"Checking model performance at {execution_date}")
    
    monitor = PerformanceMonitor()
    
    performance_results = {}
    
    for model_name in ['transformer', 'lstm', 'xgboost', 'rl']:
        metrics = monitor.get_performance_metrics(
            model_name=model_name,
            lookback_hours=24
        )
        performance_results[model_name] = metrics
        
        if metrics['accuracy'] < 0.50:
            logger.warning(f"Low accuracy for {model_name}: {metrics['accuracy']:.2%}")
    
    context['task_instance'].xcom_push(key='performance_results', value=performance_results)
    
    return performance_results


def check_data_quality(**context) -> Dict[str, Any]:
    """
    Monitor data quality and completeness.
    
    Returns:
        Data quality metrics
    """
    from monitoring.data_quality_monitor import DataQualityMonitor
    
    execution_date = context['execution_date']
    logger.info(f"Checking data quality at {execution_date}")
    
    monitor = DataQualityMonitor()
    
    quality_metrics = monitor.check_quality(
        lookback_hours=1
    )
    
    if quality_metrics['completeness'] < 0.95:
        logger.warning(f"Low data completeness: {quality_metrics['completeness']:.2%}")
    
    if quality_metrics['missing_symbols'] > 5:
        logger.warning(f"Missing data for {quality_metrics['missing_symbols']} symbols")
    
    context['task_instance'].xcom_push(key='quality_metrics', value=quality_metrics)
    
    return quality_metrics


def check_system_health(**context) -> Dict[str, Any]:
    """
    Monitor system resource usage and health.
    
    Returns:
        System health metrics
    """
    from monitoring.system_health import SystemHealthMonitor
    
    execution_date = context['execution_date']
    logger.info(f"Checking system health at {execution_date}")
    
    monitor = SystemHealthMonitor()
    
    health_metrics = monitor.check_health()
    
    # Check critical thresholds
    if health_metrics['cpu_usage'] > 90:
        logger.error(f"Critical CPU usage: {health_metrics['cpu_usage']:.1f}%")
    
    if health_metrics['memory_usage'] > 90:
        logger.error(f"Critical memory usage: {health_metrics['memory_usage']:.1f}%")
    
    if health_metrics['disk_usage'] > 85:
        logger.warning(f"High disk usage: {health_metrics['disk_usage']:.1f}%")
    
    context['task_instance'].xcom_push(key='health_metrics', value=health_metrics)
    
    return health_metrics


def check_trading_performance(**context) -> Dict[str, Any]:
    """
    Monitor trading performance and risk metrics.
    
    Returns:
        Trading performance metrics
    """
    from monitoring.trading_monitor import TradingMonitor
    
    execution_date = context['execution_date']
    logger.info(f"Checking trading performance at {execution_date}")
    
    monitor = TradingMonitor()
    
    trading_metrics = monitor.get_performance_metrics(
        lookback_days=7
    )
    
    # Check risk thresholds
    if trading_metrics['current_drawdown'] > 0.15:
        logger.error(f"High drawdown: {trading_metrics['current_drawdown']:.2%}")
    
    if trading_metrics['sharpe_ratio'] < 0.5:
        logger.warning(f"Low Sharpe ratio: {trading_metrics['sharpe_ratio']:.2f}")
    
    if trading_metrics['win_rate'] < 0.40:
        logger.warning(f"Low win rate: {trading_metrics['win_rate']:.2%}")
    
    context['task_instance'].xcom_push(key='trading_metrics', value=trading_metrics)
    
    return trading_metrics


def trigger_retraining_if_needed(**context) -> str:
    """
    Trigger model retraining if drift or performance issues detected.
    
    Returns:
        'retrain' or 'skip'
    """
    from airflow.operators.trigger_dagrun import TriggerDagRunOperator
    
    execution_date = context['execution_date']
    logger.info(f"Checking retraining trigger at {execution_date}")
    
    # Get monitoring results
    drift_results = context['task_instance'].xcom_pull(
        key='drift_results',
        task_ids='check_model_drift'
    )
    performance_results = context['task_instance'].xcom_pull(
        key='performance_results',
        task_ids='check_model_performance'
    )
    
    # Check if any model needs retraining
    needs_retraining = False
    
    for model_name in ['transformer', 'lstm', 'xgboost', 'rl']:
        drift_detected = drift_results[model_name]['drift_detected']
        accuracy = performance_results[model_name]['accuracy']
        
        if drift_detected or accuracy < 0.50:
            logger.warning(f"{model_name} needs retraining (drift={drift_detected}, acc={accuracy:.2%})")
            needs_retraining = True
    
    if needs_retraining:
        logger.info("Triggering retraining pipeline")
        return 'retrain'
    else:
        logger.info("No retraining needed")
        return 'skip'


def send_monitoring_report(**context) -> None:
    """
    Send monitoring report via email/Slack.
    """
    from monitoring.reporter import MonitoringReporter
    
    execution_date = context['execution_date']
    logger.info(f"Sending monitoring report for {execution_date}")
    
    # Get all monitoring results
    drift_results = context['task_instance'].xcom_pull(
        key='drift_results',
        task_ids='check_model_drift'
    )
    performance_results = context['task_instance'].xcom_pull(
        key='performance_results',
        task_ids='check_model_performance'
    )
    quality_metrics = context['task_instance'].xcom_pull(
        key='quality_metrics',
        task_ids='check_data_quality'
    )
    health_metrics = context['task_instance'].xcom_pull(
        key='health_metrics',
        task_ids='check_system_health'
    )
    trading_metrics = context['task_instance'].xcom_pull(
        key='trading_metrics',
        task_ids='check_trading_performance'
    )
    
    # Generate and send report
    reporter = MonitoringReporter()
    reporter.send_report(
        execution_date=execution_date,
        drift_results=drift_results,
        performance_results=performance_results,
        quality_metrics=quality_metrics,
        health_metrics=health_metrics,
        trading_metrics=trading_metrics
    )
    
    logger.info("Monitoring report sent")


# Define DAG
with DAG(
    dag_id='monitoring_pipeline',
    default_args=default_args,
    description='Monitor system health and performance',
    schedule_interval='0 * * * *',  # Every hour
    catchup=False,
    max_active_runs=1,
    tags=['monitoring', 'health', 'alerts'],
) as dag:
    
    # Start
    start = BashOperator(
        task_id='start',
        bash_command='echo "Starting monitoring pipeline"'
    )
    
    # Check model drift
    drift_check = PythonOperator(
        task_id='check_model_drift',
        python_callable=check_model_drift,
        provide_context=True,
    )
    
    # Check model performance
    performance_check = PythonOperator(
        task_id='check_model_performance',
        python_callable=check_model_performance,
        provide_context=True,
    )
    
    # Check data quality
    quality_check = PythonOperator(
        task_id='check_data_quality',
        python_callable=check_data_quality,
        provide_context=True,
    )
    
    # Check system health
    health_check = PythonOperator(
        task_id='check_system_health',
        python_callable=check_system_health,
        provide_context=True,
    )
    
    # Check trading performance
    trading_check = PythonOperator(
        task_id='check_trading_performance',
        python_callable=check_trading_performance,
        provide_context=True,
    )
    
    # Trigger retraining if needed
    retrain_trigger = PythonOperator(
        task_id='trigger_retraining_if_needed',
        python_callable=trigger_retraining_if_needed,
        provide_context=True,
    )
    
    # Send report
    send_report = PythonOperator(
        task_id='send_monitoring_report',
        python_callable=send_monitoring_report,
        provide_context=True,
    )
    
    # End
    end = BashOperator(
        task_id='end',
        bash_command='echo "Monitoring pipeline complete"'
    )
    
    # Define dependencies
    start >> [drift_check, performance_check, quality_check, health_check, trading_check]
    [drift_check, performance_check] >> retrain_trigger
    [quality_check, health_check, trading_check, retrain_trigger] >> send_report
    send_report >> end
