"""
Training DAG

Orchestrates model training pipeline with data preparation, 
training, validation, and deployment.
Runs daily at 2:00 AM UTC.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List

from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.bash import BashOperator
from airflow.operators.docker_operator import DockerOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.sensors.external_task import ExternalTaskSensor

import logging

logger = logging.getLogger(__name__)


default_args = {
    'owner': 'chronox',
    'depends_on_past': True,
    'start_date': datetime(2024, 1, 1),
    'email': ['alerts@chronox.ai'],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=15),
}


def prepare_training_data(**context) -> Dict[str, Any]:
    """
    Prepare and preprocess training data.
    
    Returns:
        Dictionary with dataset statistics
    """
    from training.data_preparation import TrainingDataPreparator
    from pathlib import Path
    
    execution_date = context['execution_date']
    logger.info(f"Preparing training data for {execution_date}")
    
    preparator = TrainingDataPreparator()
    
    # Define lookback period (90 days)
    lookback_days = 90
    end_date = execution_date
    start_date = end_date - timedelta(days=lookback_days)
    
    # Fetch and prepare data
    dataset_stats = preparator.prepare_dataset(
        start_date=start_date,
        end_date=end_date,
        val_split=0.15,
        test_split=0.15
    )
    
    logger.info(f"Dataset prepared: {dataset_stats}")
    
    # Save dataset metadata
    dataset_path = Path(f"/app/data/datasets/{execution_date.strftime('%Y%m%d')}")
    dataset_path.mkdir(parents=True, exist_ok=True)
    
    context['task_instance'].xcom_push(key='dataset_stats', value=dataset_stats)
    context['task_instance'].xcom_push(key='dataset_path', value=str(dataset_path))
    
    return dataset_stats


def check_training_trigger(**context) -> str:
    """
    Determine if training should proceed.
    
    Returns:
        'train' or 'skip_training'
    """
    from training.training_trigger import TrainingTrigger
    
    execution_date = context['execution_date']
    logger.info(f"Checking training trigger for {execution_date}")
    
    trigger = TrainingTrigger()
    
    # Check if retraining is needed
    should_train = trigger.should_retrain(
        check_drift=True,
        check_performance=True,
        check_schedule=True
    )
    
    if should_train:
        logger.info("Training triggered")
        return 'train_transformer_model'
    else:
        logger.info("Training skipped - no trigger conditions met")
        return 'skip_training'


def train_transformer_model(**context) -> Dict[str, Any]:
    """
    Train Transformer model.
    
    Returns:
        Training metrics and model path
    """
    from training.models.transformer_trainer import TransformerTrainer
    from training.config import get_transformer_config
    
    execution_date = context['execution_date']
    dataset_path = context['task_instance'].xcom_pull(
        key='dataset_path',
        task_ids='prepare_training_data'
    )
    
    logger.info(f"Training Transformer model for {execution_date}")
    
    config = get_transformer_config()
    trainer = TransformerTrainer(config)
    
    # Train model
    results = trainer.train(
        dataset_path=dataset_path,
        max_epochs=100,
        early_stopping_patience=10,
        use_mixed_precision=True,
        gradient_checkpointing=True
    )
    
    logger.info(f"Transformer training complete: {results['metrics']}")
    
    context['task_instance'].xcom_push(key='transformer_results', value=results)
    
    return results


def train_lstm_model(**context) -> Dict[str, Any]:
    """
    Train LSTM model.
    
    Returns:
        Training metrics and model path
    """
    from training.models.lstm_trainer import LSTMTrainer
    from training.config import get_lstm_config
    
    execution_date = context['execution_date']
    dataset_path = context['task_instance'].xcom_pull(
        key='dataset_path',
        task_ids='prepare_training_data'
    )
    
    logger.info(f"Training LSTM model for {execution_date}")
    
    config = get_lstm_config()
    trainer = LSTMTrainer(config)
    
    results = trainer.train(
        dataset_path=dataset_path,
        max_epochs=100,
        early_stopping_patience=10,
        use_mixed_precision=True
    )
    
    logger.info(f"LSTM training complete: {results['metrics']}")
    
    context['task_instance'].xcom_push(key='lstm_results', value=results)
    
    return results


def train_xgboost_model(**context) -> Dict[str, Any]:
    """
    Train XGBoost model.
    
    Returns:
        Training metrics and model path
    """
    from training.models.xgboost_trainer import XGBoostTrainer
    from training.config import get_xgboost_config
    
    execution_date = context['execution_date']
    dataset_path = context['task_instance'].xcom_pull(
        key='dataset_path',
        task_ids='prepare_training_data'
    )
    
    logger.info(f"Training XGBoost model for {execution_date}")
    
    config = get_xgboost_config()
    trainer = XGBoostTrainer(config)
    
    results = trainer.train(
        dataset_path=dataset_path,
        num_boost_rounds=1000,
        early_stopping_rounds=50
    )
    
    logger.info(f"XGBoost training complete: {results['metrics']}")
    
    context['task_instance'].xcom_push(key='xgboost_results', value=results)
    
    return results


def train_rl_agent(**context) -> Dict[str, Any]:
    """
    Train RL agent (PPO).
    
    Returns:
        Training metrics and agent path
    """
    from training.rl.ppo_trainer import PPOTrainer
    from training.config import get_rl_config
    
    execution_date = context['execution_date']
    dataset_path = context['task_instance'].xcom_pull(
        key='dataset_path',
        task_ids='prepare_training_data'
    )
    
    logger.info(f"Training RL agent for {execution_date}")
    
    config = get_rl_config()
    trainer = PPOTrainer(config)
    
    results = trainer.train(
        dataset_path=dataset_path,
        total_timesteps=1_000_000,
        eval_freq=10_000
    )
    
    logger.info(f"RL training complete: {results['metrics']}")
    
    context['task_instance'].xcom_push(key='rl_results', value=results)
    
    return results


def validate_models(**context) -> Dict[str, Any]:
    """
    Validate all trained models.
    
    Returns:
        Validation results for all models
    """
    from validation.model_validator import ModelValidator
    
    execution_date = context['execution_date']
    logger.info(f"Validating models for {execution_date}")
    
    validator = ModelValidator()
    
    # Get model results from XCom
    transformer_results = context['task_instance'].xcom_pull(
        key='transformer_results',
        task_ids='train_transformer_model'
    )
    lstm_results = context['task_instance'].xcom_pull(
        key='lstm_results',
        task_ids='train_lstm_model'
    )
    xgboost_results = context['task_instance'].xcom_pull(
        key='xgboost_results',
        task_ids='train_xgboost_model'
    )
    rl_results = context['task_instance'].xcom_pull(
        key='rl_results',
        task_ids='train_rl_agent'
    )
    
    # Validate each model
    validation_results = {}
    
    for model_name, results in [
        ('transformer', transformer_results),
        ('lstm', lstm_results),
        ('xgboost', xgboost_results),
        ('rl', rl_results)
    ]:
        val_result = validator.validate_model(
            model_path=results['model_path'],
            model_type=model_name,
            min_accuracy=0.55,
            min_sharpe=0.8
        )
        validation_results[model_name] = val_result
    
    logger.info(f"Validation complete: {validation_results}")
    
    context['task_instance'].xcom_push(key='validation_results', value=validation_results)
    
    return validation_results


def run_backtests(**context) -> Dict[str, Any]:
    """
    Run comprehensive backtests on validated models.
    
    Returns:
        Backtest results for all models
    """
    from backtesting.backtest_runner import BacktestRunner
    from backtesting.config import get_backtest_config
    
    execution_date = context['execution_date']
    logger.info(f"Running backtests for {execution_date}")
    
    config = get_backtest_config()
    runner = BacktestRunner(config)
    
    # Get model results
    transformer_results = context['task_instance'].xcom_pull(
        key='transformer_results',
        task_ids='train_transformer_model'
    )
    lstm_results = context['task_instance'].xcom_pull(
        key='lstm_results',
        task_ids='train_lstm_model'
    )
    xgboost_results = context['task_instance'].xcom_pull(
        key='xgboost_results',
        task_ids='train_xgboost_model'
    )
    rl_results = context['task_instance'].xcom_pull(
        key='rl_results',
        task_ids='train_rl_agent'
    )
    
    # Run backtests
    backtest_results = {}
    
    for model_name, results in [
        ('transformer', transformer_results),
        ('lstm', lstm_results),
        ('xgboost', xgboost_results),
        ('rl', rl_results)
    ]:
        bt_result = runner.run_backtest(
            model_path=results['model_path'],
            model_type=model_name,
            start_date=execution_date - timedelta(days=30),
            end_date=execution_date,
            initial_capital=100_000
        )
        backtest_results[model_name] = bt_result
    
    logger.info(f"Backtests complete")
    
    context['task_instance'].xcom_push(key='backtest_results', value=backtest_results)
    
    return backtest_results


def deploy_models(**context) -> Dict[str, str]:
    """
    Deploy validated models to production.
    
    Returns:
        Deployment status for each model
    """
    from deployment.model_deployer import ModelDeployer
    
    execution_date = context['execution_date']
    logger.info(f"Deploying models for {execution_date}")
    
    deployer = ModelDeployer()
    
    # Get validation results
    validation_results = context['task_instance'].xcom_pull(
        key='validation_results',
        task_ids='validate_models'
    )
    
    # Get backtest results
    backtest_results = context['task_instance'].xcom_pull(
        key='backtest_results',
        task_ids='run_backtests'
    )
    
    # Deploy models that passed validation
    deployment_status = {}
    
    for model_name in ['transformer', 'lstm', 'xgboost', 'rl']:
        val_passed = validation_results[model_name]['passed']
        sharpe_ratio = backtest_results[model_name]['sharpe_ratio']
        
        if val_passed and sharpe_ratio > 1.0:
            status = deployer.deploy_model(
                model_name=model_name,
                execution_date=execution_date
            )
            deployment_status[model_name] = 'deployed'
            logger.info(f"Deployed {model_name} model")
        else:
            deployment_status[model_name] = 'rejected'
            logger.warning(f"Rejected {model_name} model (validation or performance)")
    
    context['task_instance'].xcom_push(key='deployment_status', value=deployment_status)
    
    return deployment_status


def cleanup_old_models(**context) -> int:
    """
    Clean up old model checkpoints.
    
    Returns:
        Number of models deleted
    """
    from deployment.model_cleanup import ModelCleanup
    
    logger.info("Cleaning up old models")
    
    cleanup = ModelCleanup()
    deleted_count = cleanup.cleanup_old_checkpoints(
        keep_last_n=10,
        delete_older_than_days=30
    )
    
    logger.info(f"Deleted {deleted_count} old model files")
    return deleted_count


# Define DAG
with DAG(
    dag_id='training_pipeline',
    default_args=default_args,
    description='Train and validate ML models',
    schedule_interval='0 2 * * *',  # Daily at 2:00 AM UTC
    catchup=False,
    max_active_runs=1,
    tags=['training', 'ml', 'models'],
) as dag:
    
    # Start
    start = BashOperator(
        task_id='start',
        bash_command='echo "Starting training pipeline"'
    )
    
    # Prepare data
    prepare_data = PythonOperator(
        task_id='prepare_training_data',
        python_callable=prepare_training_data,
        provide_context=True,
    )
    
    # Check if training should proceed
    check_trigger = BranchPythonOperator(
        task_id='check_training_trigger',
        python_callable=check_training_trigger,
        provide_context=True,
    )
    
    # Train models in parallel
    train_transformer = PythonOperator(
        task_id='train_transformer_model',
        python_callable=train_transformer_model,
        provide_context=True,
    )
    
    train_lstm = PythonOperator(
        task_id='train_lstm_model',
        python_callable=train_lstm_model,
        provide_context=True,
    )
    
    train_xgboost = PythonOperator(
        task_id='train_xgboost_model',
        python_callable=train_xgboost_model,
        provide_context=True,
    )
    
    train_rl = PythonOperator(
        task_id='train_rl_agent',
        python_callable=train_rl_agent,
        provide_context=True,
    )
    
    # Validate models
    validate = PythonOperator(
        task_id='validate_models',
        python_callable=validate_models,
        provide_context=True,
        trigger_rule='all_done',
    )
    
    # Run backtests
    backtest = PythonOperator(
        task_id='run_backtests',
        python_callable=run_backtests,
        provide_context=True,
    )
    
    # Deploy models
    deploy = PythonOperator(
        task_id='deploy_models',
        python_callable=deploy_models,
        provide_context=True,
    )
    
    # Cleanup
    cleanup = PythonOperator(
        task_id='cleanup_old_models',
        python_callable=cleanup_old_models,
        provide_context=True,
    )
    
    # Skip training branch
    skip = BashOperator(
        task_id='skip_training',
        bash_command='echo "Training skipped"'
    )
    
    # End
    end = BashOperator(
        task_id='end',
        bash_command='echo "Training pipeline complete"',
        trigger_rule='none_failed_min_one_success',
    )
    
    # Define task dependencies
    start >> prepare_data >> check_trigger
    check_trigger >> [train_transformer, train_lstm, train_xgboost, train_rl]
    check_trigger >> skip
    [train_transformer, train_lstm, train_xgboost, train_rl] >> validate
    validate >> backtest >> deploy >> cleanup
    [skip, cleanup] >> end
