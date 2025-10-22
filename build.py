"""
ChronoX Build & Test Script

This script provides commands to build, test, and run ChronoX.
"""

import sys
import subprocess
from pathlib import Path
from typing import List, Optional
import argparse


class ChronoXBuilder:
    """Build and test ChronoX components."""
    
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.docker_dir = project_root / "docker"
        
    def run_command(self, cmd: List[str], cwd: Optional[Path] = None) -> int:
        """Run a shell command and return exit code."""
        print(f"\n{'='*80}")
        print(f"Running: {' '.join(cmd)}")
        print(f"{'='*80}\n")
        
        result = subprocess.run(
            cmd,
            cwd=cwd or self.project_root,
            check=False
        )
        return result.returncode
    
    def build_docker_base(self) -> int:
        """Build the base Docker image."""
        print("\n🔨 Building Docker base image...")
        return self.run_command([
            "docker", "build",
            "-t", "chronox-base:latest",
            "-f", str(self.docker_dir / "Dockerfile.base"),
            "."
        ])
    
    def build_docker_training(self) -> int:
        """Build the training Docker image."""
        print("\n🔨 Building Docker training image...")
        return self.run_command([
            "docker", "build",
            "-t", "chronox-training:latest",
            "-f", str(self.docker_dir / "Dockerfile.training"),
            "."
        ])
    
    def build_docker_inference(self) -> int:
        """Build the inference Docker image."""
        print("\n🔨 Building Docker inference image...")
        return self.run_command([
            "docker", "build",
            "-t", "chronox-inference:latest",
            "-f", str(self.docker_dir / "Dockerfile.inference"),
            "."
        ])
    
    def build_all_docker(self) -> int:
        """Build all Docker images."""
        print("\n🚀 Building all Docker images...\n")
        
        # Build in order (base first)
        if self.build_docker_base() != 0:
            print("❌ Failed to build base image")
            return 1
        
        # Build training and inference in parallel would be faster,
        # but sequential is safer for first build
        if self.build_docker_training() != 0:
            print("❌ Failed to build training image")
            return 1
        
        if self.build_docker_inference() != 0:
            print("❌ Failed to build inference image")
            return 1
        
        print("\n✅ All Docker images built successfully!")
        return 0
    
    def start_services(self, services: Optional[List[str]] = None) -> int:
        """Start Docker Compose services."""
        cmd = ["docker-compose", "up", "-d"]
        if services:
            cmd.extend(services)
        else:
            print("\n🚀 Starting all services...")
        
        return self.run_command(cmd, cwd=self.docker_dir.parent)
    
    def stop_services(self) -> int:
        """Stop Docker Compose services."""
        print("\n🛑 Stopping all services...")
        return self.run_command(
            ["docker-compose", "down"],
            cwd=self.docker_dir.parent
        )
    
    def check_services(self) -> int:
        """Check status of Docker Compose services."""
        print("\n📊 Checking service status...")
        return self.run_command(
            ["docker-compose", "ps"],
            cwd=self.docker_dir.parent
        )
    
    def view_logs(self, service: Optional[str] = None, follow: bool = False) -> int:
        """View logs from Docker Compose services."""
        cmd = ["docker-compose", "logs"]
        if follow:
            cmd.append("-f")
        if service:
            cmd.append(service)
        
        return self.run_command(cmd, cwd=self.docker_dir.parent)
    
    def run_tests(self, test_path: Optional[str] = None, verbose: bool = True) -> int:
        """Run pytest tests."""
        print("\n🧪 Running tests...")
        
        cmd = ["pytest"]
        
        if verbose:
            cmd.append("-v")
        
        cmd.extend([
            "--tb=short",
            "--color=yes",
        ])
        
        if test_path:
            cmd.append(test_path)
        else:
            cmd.append("tests/")
        
        return self.run_command(cmd)
    
    def run_tests_with_coverage(self) -> int:
        """Run tests with coverage report."""
        print("\n🧪 Running tests with coverage...")
        
        return self.run_command([
            "pytest",
            "tests/",
            "--cov=.",
            "--cov-report=html",
            "--cov-report=term-missing",
            "-v"
        ])
    
    def lint_code(self) -> int:
        """Run code linting."""
        print("\n🔍 Linting code...")
        
        # Run black
        print("\n▶️  Running Black...")
        black_result = self.run_command(["black", "--check", "."])
        
        # Run isort
        print("\n▶️  Running isort...")
        isort_result = self.run_command(["isort", "--check-only", "."])
        
        # Run flake8
        print("\n▶️  Running flake8...")
        flake8_result = self.run_command(["flake8", "."])
        
        if black_result != 0 or isort_result != 0 or flake8_result != 0:
            print("\n❌ Linting failed!")
            return 1
        
        print("\n✅ All linting checks passed!")
        return 0
    
    def format_code(self) -> int:
        """Format code with black and isort."""
        print("\n✨ Formatting code...")
        
        # Run black
        print("\n▶️  Running Black...")
        self.run_command(["black", "."])
        
        # Run isort
        print("\n▶️  Running isort...")
        self.run_command(["isort", "."])
        
        print("\n✅ Code formatted!")
        return 0
    
    def setup_dev_environment(self) -> int:
        """Set up development environment."""
        print("\n🛠️  Setting up development environment...")
        
        # Check if venv exists
        venv_path = self.project_root / ".venv"
        if not venv_path.exists():
            print("Creating virtual environment...")
            self.run_command([sys.executable, "-m", "venv", ".venv"])
        
        # Install requirements
        print("\nInstalling base requirements...")
        pip_cmd = str(venv_path / "bin" / "pip")
        
        self.run_command([
            pip_cmd, "install", "-r", "requirements/base.txt"
        ])
        
        print("\nInstalling training requirements...")
        self.run_command([
            pip_cmd, "install", "-r", "requirements/training.txt"
        ])
        
        print("\n✅ Development environment ready!")
        return 0
    
    def init_database(self) -> int:
        """Initialize TimescaleDB."""
        print("\n🗄️  Initializing TimescaleDB...")
        
        # Start TimescaleDB service
        self.run_command(
            ["docker-compose", "up", "-d", "timescaledb"],
            cwd=self.docker_dir.parent
        )
        
        # Wait for database to be ready
        print("\nWaiting for TimescaleDB to be ready...")
        import time
        time.sleep(10)
        
        # Run initialization script
        print("\nRunning initialization script...")
        self.run_command([
            "docker", "exec", "chronox-timescaledb",
            "psql", "-U", "chronox", "-d", "chronox_timeseries",
            "-f", "/docker-entrypoint-initdb.d/01_init.sql"
        ])
        
        print("\n✅ TimescaleDB initialized!")
        return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ChronoX Build & Test Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build all Docker images
  python build.py build-all
  
  # Run all tests
  python build.py test
  
  # Start all services
  python build.py start
  
  # Check service status
  python build.py status
  
  # View logs
  python build.py logs
  
  # Format code
  python build.py format
        """
    )
    
    parser.add_argument(
        "command",
        choices=[
            "build-base", "build-training", "build-inference", "build-all",
            "start", "stop", "restart", "status", "logs",
            "test", "test-cov", "lint", "format",
            "setup", "init-db", "clean"
        ],
        help="Command to execute"
    )
    
    parser.add_argument(
        "--service",
        help="Specific service name (for logs, start, etc.)"
    )
    
    parser.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Follow logs (tail -f style)"
    )
    
    parser.add_argument(
        "--test-path",
        help="Specific test file or directory to run"
    )
    
    args = parser.parse_args()
    
    # Get project root
    project_root = Path(__file__).parent.absolute()
    builder = ChronoXBuilder(project_root)
    
    # Execute command
    try:
        if args.command == "build-base":
            exit_code = builder.build_docker_base()
        elif args.command == "build-training":
            exit_code = builder.build_docker_training()
        elif args.command == "build-inference":
            exit_code = builder.build_docker_inference()
        elif args.command == "build-all":
            exit_code = builder.build_all_docker()
        elif args.command == "start":
            services = [args.service] if args.service else None
            exit_code = builder.start_services(services)
        elif args.command == "stop":
            exit_code = builder.stop_services()
        elif args.command == "restart":
            builder.stop_services()
            services = [args.service] if args.service else None
            exit_code = builder.start_services(services)
        elif args.command == "status":
            exit_code = builder.check_services()
        elif args.command == "logs":
            exit_code = builder.view_logs(args.service, args.follow)
        elif args.command == "test":
            exit_code = builder.run_tests(args.test_path)
        elif args.command == "test-cov":
            exit_code = builder.run_tests_with_coverage()
        elif args.command == "lint":
            exit_code = builder.lint_code()
        elif args.command == "format":
            exit_code = builder.format_code()
        elif args.command == "setup":
            exit_code = builder.setup_dev_environment()
        elif args.command == "init-db":
            exit_code = builder.init_database()
        elif args.command == "clean":
            print("\n🧹 Cleaning up...")
            builder.stop_services()
            builder.run_command(["docker-compose", "down", "-v"])
            exit_code = 0
        else:
            print(f"Unknown command: {args.command}")
            exit_code = 1
        
        sys.exit(exit_code)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
