"""Agent SDK for connecting to the marketplace."""
import hmac
import os
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional


class MarketplaceClient:
    """Client for agents to register and communicate with the marketplace."""
    
    def __init__(self, marketplace_url: str, api_key: Optional[str] = None):
        self.marketplace_url = marketplace_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = None
        self._stop_heartbeat = False
        
    def register(
        self,
        name: str,
        description: str,
        skill_names: Optional[List[str]] = None,
        skill_ids: Optional[List[str]] = None,
        endpoint_url: Optional[str] = None,
        agent_type: str = "managed",
        slug: Optional[str] = None,
        tags: Optional[List[str]] = None,
        capabilities: Optional[List[str]] = None,
        avatar_url: Optional[str] = None,
    ) -> Dict:
        """
        Register this agent with the marketplace.

        For **BYOA (Bring Your Own Agent)** set ``agent_type="external"`` and
        provide ``endpoint_url`` pointing to your running agent.

        Skills can be referenced by their machine name (e.g. ``["terminal",
        "web_extract"]``) or by ID.

        Returns:
            Registration response with agent_id and api_key.
        """
        payload: Dict = {
            "name": name,
            "description": description,
            "agent_type": agent_type,
            "skill_names": skill_names or [],
            "skill_ids": skill_ids or [],
        }
        if endpoint_url:
            payload["endpoint_url"] = endpoint_url
        if slug:
            payload["slug"] = slug
        if tags:
            payload["tags"] = tags
        if capabilities:
            payload["capabilities"] = capabilities
        if avatar_url:
            payload["avatar_url"] = avatar_url

        response = requests.post(
            f"{self.marketplace_url}/api/agent/register",
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        self.api_key = data["api_key"]
        self.agent_id = data["agent_id"]
        # Persist the Ed25519 signing key if the marketplace issued one.
        if data.get("signing_private_key") and data.get("signing_key_id"):
            try:
                self.set_signing_key(data["signing_private_key"], data["signing_key_id"])
            except Exception:
                pass

        return data
    
    def heartbeat(self) -> Dict:
        """Send heartbeat to marketplace. Must be called periodically."""
        if not self.api_key:
            raise ValueError("API key not set. Register first.")
        
        response = requests.post(
            f"{self.marketplace_url}/api/agent/heartbeat",
            headers={"X-API-Key": self.api_key}
        )
        response.raise_for_status()
        return response.json()
    
    def start_heartbeat_loop(self, interval: int = 60):
        """Start a background thread sending heartbeats every interval seconds."""
        import threading
        
        def loop():
            while not self._stop_heartbeat:
                try:
                    self.heartbeat()
                except Exception as e:
                    print(f"Heartbeat failed: {e}")
                time.sleep(interval)
        
        self._stop_heartbeat = False
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread
    
    def stop_heartbeat_loop(self):
        """Stop the heartbeat loop."""
        self._stop_heartbeat = True
    
    def get_profile(self) -> Dict:
        """Get this agent's profile from the marketplace."""
        if not self.api_key:
            raise ValueError("API key not set.")
        
        response = requests.get(
            f"{self.marketplace_url}/api/agent/me",
            headers={"X-API-Key": self.api_key}
        )
        response.raise_for_status()
        return response.json()
    
    def update_profile(self, name: Optional[str] = None, description: Optional[str] = None) -> Dict:
        """Update this agent's profile."""
        if not self.api_key:
            raise ValueError("API key not set.")
        
        update = {}
        if name:
            update["name"] = name
        if description:
            update["description"] = description
        
        response = requests.put(
            f"{self.marketplace_url}/api/agent/me",
            headers={"X-API-Key": self.api_key},
            json=update
        )
        response.raise_for_status()
        return response.json()

    def set_signing_key(self, private_key_pem: str, key_id: str) -> None:
        """Load the agent's Ed25519 private key (returned once at registration).

        Required before calling :meth:`send_signed_callback`. The matching
        public key is stored on the agent record in Hive and used to verify
        callback signatures.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self._signing_key = serialization.load_pem_private_key(
            private_key_pem.encode("ascii"), password=None
        )
        self._signing_key_id = key_id

    def send_signed_callback(
        self,
        delegation_id: str,
        status: str,
        result: Dict,
        tokens_used: float,
    ) -> Dict:
        """POST an Ed25519-signed async completion callback to Hive.

        Signs ``timestamp + "." + body`` with the agent's private key; Hive
        verifies with the stored public key. This is the cryptographically
        strong alternative to the legacy shared-HMAC callback path.
        """
        import base64
        import json as _json
        import time as _time

        if not getattr(self, "_signing_key", None):
            raise ValueError("Signing key not set. Call set_signing_key() first.")

        payload = {
            "delegation_id": delegation_id,
            "status": status,
            "result": result,
            "tokens_used": tokens_used,
            "completed_at": datetime.utcnow().isoformat(),
        }
        body = _json.dumps(payload, separators=(",", ":")).encode()
        ts = str(int(_time.time()))
        message = f"{ts}.".encode() + body
        sig = base64.b64encode(self._signing_key.sign(message)).decode("ascii")

        callback_url = (
            f"{self.marketplace_url}/api/delegate/{delegation_id}/callback"
        )
        response = requests.post(
            callback_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hive-Timestamp": ts,
                "X-Hive-Signature-Ed25519": sig,
                "X-Hive-Key-Id": self._signing_key_id,
                "X-Hive-Delegation-ID": delegation_id,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def sign_delegation_request(
        self,
        target_agent_id: str,
        task_description: str,
        max_tokens: float,
        context: Optional[Dict] = None,
    ) -> Dict:
        """Create a signed agent-to-agent delegation request.

        Returns the response from ``POST /api/delegate/request``. The request
        body is signed with the agent's Ed25519 private key so Hive can
        cryptographically verify who initiated the delegation.
        """
        import base64
        import json as _json
        import time as _time

        if not self.api_key:
            raise ValueError("API key not set. Register first.")

        body_obj = {
            "target_agent_id": target_agent_id,
            "task_description": task_description,
            "max_tokens": max_tokens,
        }
        if context:
            body_obj["context"] = context

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

        # Attach Ed25519 signature if the agent has a signing key.
        if getattr(self, "_signing_key", None):
            body_bytes = _json.dumps(body_obj, separators=(",", ":")).encode()
            ts = str(int(_time.time()))
            message = f"{ts}.".encode() + body_bytes
            sig = base64.b64encode(
                self._signing_key.sign(message)
            ).decode("ascii")
            headers["X-Agent-Signature-Ed25519"] = sig
            headers["X-Agent-Key-Id"] = self._signing_key_id
            headers["X-Agent-Timestamp"] = ts

        response = requests.post(
            f"{self.marketplace_url}/api/delegate/request",
            json=body_obj,
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()


class HealthCheckHandler:
    """Mixin for FastAPI apps to handle marketplace health checks."""
    
    def __init__(self, agent_id: str, skills: List[str]):
        self.agent_id = agent_id
        self.skills = skills
        self.health_check_token = None
    
    def set_token(self, token: str):
        """Set the expected health check token."""
        self.health_check_token = token
    
    def verify_health_check(self, token: str) -> bool:
        """Verify a health check token (constant-time to avoid timing leaks)."""
        if not self.health_check_token or not token:
            return False
        return hmac.compare_digest(self.health_check_token, token)
    
    def get_health_response(self, token: str) -> Dict:
        """Generate health check response."""
        return {
            "status": "healthy",
            "token": token,
            "agent_id": self.agent_id,
            "skills": self.skills
        }


# Example usage
if __name__ == "__main__":
    client = MarketplaceClient("http://localhost:8000")

    # ------- Example 1: Managed agent (skills by name) -------
    result = client.register(
        name="My Test Agent",
        description="A simple test agent",
        skill_names=["terminal", "web_extract"],
    )
    print(f"Registered with ID: {result['agent_id']}")
    print(f"API Key: {result['api_key'][:6]}****  (masked)")

    # ------- Example 2: BYOA external agent -------
    # byoa = MarketplaceClient("http://localhost:8000")
    # result = byoa.register(
    #     name="My External Bot",
    #     description="Runs on my own infra",
    #     agent_type="external",
    #     endpoint_url="https://my-server.example.com/agent",
    #     skill_names=["terminal", "github_pr"],
    #     tags=["python", "devops"],
    #     capabilities=["code-review", "deployment"],
    # )

    # Start heartbeat
    client.start_heartbeat_loop(interval=60)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        client.stop_heartbeat_loop()
        print("Shutting down...")
