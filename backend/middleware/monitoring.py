"""Monitoring and metrics collection."""
import time
import logging
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime
import json


# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("hive")


class MonitoringMiddleware(BaseHTTPMiddleware):
    """Middleware to monitor API requests and responses."""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and collect metrics."""
        start_time = time.time()
        
        # Log request
        logger.info(f"Request: {request.method} {request.url.path}")
        
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            
            # Log response
            logger.info(
                f"Response: {response.status_code} | "
                f"Duration: {process_time:.3f}s | "
                f"Path: {request.url.path}"
            )
            
            # Add custom headers
            response.headers["X-Process-Time"] = str(process_time)
            
            return response
            
        except Exception as e:
            process_time = time.time() - start_time
            logger.error(
                f"Error: {str(e)} | "
                f"Duration: {process_time:.3f}s | "
                f"Path: {request.url.path}",
                exc_info=True
            )
            raise


# Metrics storage (in-memory for simplicity, use Redis/Prometheus in production)
class Metrics:
    """Simple metrics collector."""
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        """Reset all metrics."""
        self.requests_total = 0
        self.requests_by_status = {}
        self.requests_by_endpoint = {}
        self.delegation_count = 0
        self.delegation_success = 0
        self.delegation_failed = 0
        self.tokens_transferred = 0.0
        self.agents_registered = 0
        self.users_registered = 0
    
    def record_request(self, endpoint: str, status_code: int):
        """Record an API request."""
        self.requests_total += 1
        self.requests_by_status[status_code] = self.requests_by_status.get(status_code, 0) + 1
        self.requests_by_endpoint[endpoint] = self.requests_by_endpoint.get(endpoint, 0) + 1
    
    def record_delegation(self, success: bool, tokens: float = 0.0):
        """Record a delegation attempt."""
        self.delegation_count += 1
        if success:
            self.delegation_success += 1
            self.tokens_transferred += tokens
        else:
            self.delegation_failed += 1
    
    def record_agent_registration(self):
        """Record an agent registration."""
        self.agents_registered += 1
    
    def record_user_registration(self):
        """Record a user registration."""
        self.users_registered += 1
    
    def get_summary(self) -> dict:
        """Get metrics summary."""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "requests": {
                "total": self.requests_total,
                "by_status": self.requests_by_status,
                "top_endpoints": dict(sorted(
                    self.requests_by_endpoint.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:10])
            },
            "delegations": {
                "total": self.delegation_count,
                "successful": self.delegation_success,
                "failed": self.delegation_failed,
                "success_rate": (
                    self.delegation_success / self.delegation_count * 100
                    if self.delegation_count > 0 else 0
                )
            },
            "tokens": {
                "total_transferred": self.tokens_transferred
            },
            "registrations": {
                "users": self.users_registered,
                "agents": self.agents_registered
            }
        }

    def render_prometheus(self) -> str:
        """Render metrics in Prometheus text exposition format (v0.0.4)."""
        lines = [
            "# HELP hive_requests_total Total API requests.",
            "# TYPE hive_requests_total counter",
            f"hive_requests_total {self.requests_total}",
            "# HELP hive_delegations_total Delegation attempts.",
            "# TYPE hive_delegations_total counter",
            f"hive_delegations_total {self.delegation_count}",
            f"hive_delegations_success_total {self.delegation_success}",
            f"hive_delegations_failed_total {self.delegation_failed}",
            "# HELP hive_tokens_transferred_total Tokens transferred on successful delegations.",
            "# TYPE hive_tokens_transferred_total counter",
            f"hive_tokens_transferred_total {self.tokens_transferred}",
            "# HELP hive_agents_registered_total Agents registered.",
            "# TYPE hive_agents_registered_total counter",
            f"hive_agents_registered_total {self.agents_registered}",
            "# HELP hive_users_registered_total Users registered.",
            "# TYPE hive_users_registered_total counter",
            f"hive_users_registered_total {self.users_registered}",
        ]
        for status, count in sorted(self.requests_by_status.items()):
            lines.append(f'hive_requests_by_status{{status="{status}"}} {count}')
        return "\n".join(lines) + "\n"


# Global metrics instance
metrics = Metrics()


def log_event(event_type: str, data: dict):
    """Log a structured event."""
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "event_type": event_type,
        **data
    }
    logger.info(f"EVENT: {json.dumps(event)}")
