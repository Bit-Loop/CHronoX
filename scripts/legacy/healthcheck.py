#!/usr/bin/env python3
"""
ChronoX Health Check Script

Verifies that all services are running and responding correctly.
"""

import sys
import time
import socket
import subprocess
from typing import Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum


class HealthStatus(Enum):
    """Health check status."""
    HEALTHY = "✅"
    DEGRADED = "⚠️"
    UNHEALTHY = "❌"
    UNKNOWN = "❓"


@dataclass
class ServiceCheck:
    """Service health check result."""
    name: str
    status: HealthStatus
    message: str
    response_time_ms: float = 0


class ChronoXHealthChecker:
    """Health checker for ChronoX services."""
    
    def __init__(self):
        self.results: List[ServiceCheck] = []
    
    def check_port(self, host: str, port: int, timeout: float = 5.0) -> Tuple[bool, float]:
        """Check if a port is open and measure response time."""
        start_time = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            
            response_time = (time.time() - start_time) * 1000
            return result == 0, response_time
        except Exception:
            return False, 0
    
    def check_docker_service(self, service_name: str) -> bool:
        """Check if a Docker container is running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={service_name}", "--format", "{{.Status}}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return "Up" in result.stdout
        except Exception:
            return False
    
    def check_timescaledb(self) -> ServiceCheck:
        """Check TimescaleDB health."""
        is_open, response_time = self.check_port("localhost", 5432)
        
        if not is_open:
            return ServiceCheck(
                name="TimescaleDB",
                status=HealthStatus.UNHEALTHY,
                message="Port 5432 not accessible"
            )
        
        # Try to verify TimescaleDB extension
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "chronox-timescaledb",
                    "psql", "-U", "chronox", "-d", "chronox_timeseries",
                    "-c", "SELECT * FROM pg_extension WHERE extname='timescaledb';"
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if "timescaledb" in result.stdout:
                return ServiceCheck(
                    name="TimescaleDB",
                    status=HealthStatus.HEALTHY,
                    message=f"Running with TimescaleDB extension ({response_time:.0f}ms)",
                    response_time_ms=response_time
                )
            else:
                return ServiceCheck(
                    name="TimescaleDB",
                    status=HealthStatus.DEGRADED,
                    message="Running but TimescaleDB extension not found"
                )
        except Exception as e:
            return ServiceCheck(
                name="TimescaleDB",
                status=HealthStatus.DEGRADED,
                message=f"Port open but query failed: {str(e)[:50]}"
            )
    
    def check_postgres(self) -> ServiceCheck:
        """Check PostgreSQL (metadata) health."""
        is_running = self.check_docker_service("chronox-postgres")
        
        if not is_running:
            return ServiceCheck(
                name="PostgreSQL (Metadata)",
                status=HealthStatus.UNHEALTHY,
                message="Container not running"
            )
        
        return ServiceCheck(
            name="PostgreSQL (Metadata)",
            status=HealthStatus.HEALTHY,
            message="Container running"
        )
    
    def check_redis(self) -> ServiceCheck:
        """Check Redis health."""
        is_open, response_time = self.check_port("localhost", 6379)
        
        if not is_open:
            return ServiceCheck(
                name="Redis",
                status=HealthStatus.UNHEALTHY,
                message="Port 6379 not accessible"
            )
        
        return ServiceCheck(
            name="Redis",
            status=HealthStatus.HEALTHY,
            message=f"Running ({response_time:.0f}ms)",
            response_time_ms=response_time
        )
    
    def check_prometheus(self) -> ServiceCheck:
        """Check Prometheus health."""
        is_open, response_time = self.check_port("localhost", 9090)
        
        if not is_open:
            return ServiceCheck(
                name="Prometheus",
                status=HealthStatus.UNHEALTHY,
                message="Port 9090 not accessible"
            )
        
        return ServiceCheck(
            name="Prometheus",
            status=HealthStatus.HEALTHY,
            message=f"Running ({response_time:.0f}ms)",
            response_time_ms=response_time
        )
    
    def check_grafana(self) -> ServiceCheck:
        """Check Grafana health."""
        is_open, response_time = self.check_port("localhost", 3000)
        
        if not is_open:
            return ServiceCheck(
                name="Grafana",
                status=HealthStatus.UNHEALTHY,
                message="Port 3000 not accessible"
            )
        
        return ServiceCheck(
            name="Grafana",
            status=HealthStatus.HEALTHY,
            message=f"Running ({response_time:.0f}ms)",
            response_time_ms=response_time
        )
    
    def check_airflow(self) -> ServiceCheck:
        """Check Airflow health."""
        is_open, response_time = self.check_port("localhost", 8080)
        
        if not is_open:
            return ServiceCheck(
                name="Airflow",
                status=HealthStatus.UNHEALTHY,
                message="Port 8080 not accessible"
            )
        
        # Check if scheduler and worker are running
        scheduler_running = self.check_docker_service("chronox-airflow-scheduler")
        worker_running = self.check_docker_service("chronox-airflow-worker")
        
        if not scheduler_running or not worker_running:
            return ServiceCheck(
                name="Airflow",
                status=HealthStatus.DEGRADED,
                message="Webserver up but scheduler/worker may be down"
            )
        
        return ServiceCheck(
            name="Airflow",
            status=HealthStatus.HEALTHY,
            message=f"Webserver, scheduler, and worker running ({response_time:.0f}ms)",
            response_time_ms=response_time
        )
    
    def run_all_checks(self) -> List[ServiceCheck]:
        """Run all health checks."""
        print("🏥 ChronoX Health Check")
        print("=" * 80)
        print()
        
        checks = [
            ("Core Services", [
                self.check_timescaledb,
                self.check_postgres,
                self.check_redis,
            ]),
            ("Monitoring", [
                self.check_prometheus,
                self.check_grafana,
            ]),
            ("Orchestration", [
                self.check_airflow,
            ])
        ]
        
        all_results = []
        
        for category, check_funcs in checks:
            print(f"\n📋 {category}")
            print("-" * 80)
            
            for check_func in check_funcs:
                result = check_func()
                all_results.append(result)
                
                print(f"  {result.status.value} {result.name:30s} {result.message}")
        
        return all_results
    
    def print_summary(self, results: List[ServiceCheck]):
        """Print health check summary."""
        print()
        print("=" * 80)
        print("📊 Summary")
        print("=" * 80)
        
        healthy = sum(1 for r in results if r.status == HealthStatus.HEALTHY)
        degraded = sum(1 for r in results if r.status == HealthStatus.DEGRADED)
        unhealthy = sum(1 for r in results if r.status == HealthStatus.UNHEALTHY)
        
        print(f"  ✅ Healthy:   {healthy}/{len(results)}")
        print(f"  ⚠️  Degraded:  {degraded}/{len(results)}")
        print(f"  ❌ Unhealthy: {unhealthy}/{len(results)}")
        print()
        
        if unhealthy > 0:
            print("⚠️  Some services are unhealthy. Please check the logs.")
            print("   Run: docker-compose logs [service-name]")
            print()
            return False
        elif degraded > 0:
            print("⚠️  All critical services are running, but some are degraded.")
            print()
            return True
        else:
            print("🎉 All services are healthy!")
            print()
            return True


def main():
    """Main entry point."""
    checker = ChronoXHealthChecker()
    
    try:
        results = checker.run_all_checks()
        success = checker.print_summary(results)
        
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Health check interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Error during health check: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
