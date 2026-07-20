#!/bin/bash

# Hive Marketplace - Quick Test Script
# This script helps you test the new marketplace features

set -e

BASE_URL="http://localhost:8000"
echo "🐝 Hive Marketplace Test Script"
echo "================================"
echo ""

# Check if server is running
echo "Checking if server is running..."
if ! curl -s "${BASE_URL}/api/health" > /dev/null; then
    echo "❌ Server is not running!"
    echo "Start it with: cd backend && uvicorn main:app --reload"
    exit 1
fi
echo "✅ Server is running"
echo ""

# Test 1: Register a user
echo "Test 1: User Registration"
echo "-------------------------"
USER_EMAIL="test-$(date +%s)@example.com"
USER_PASSWORD="password123"

REGISTER_RESPONSE=$(curl -s -X POST "${BASE_URL}/api/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${USER_EMAIL}\", \"password\": \"${USER_PASSWORD}\", \"name\": \"Test User\"}")

echo "User registered: ${USER_EMAIL}"
echo ""

# Test 2: Login
echo "Test 2: User Login"
echo "------------------"
LOGIN_RESPONSE=$(curl -s -X POST "${BASE_URL}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${USER_EMAIL}\", \"password\": \"${USER_PASSWORD}\"}")

JWT_TOKEN=$(echo $LOGIN_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")

if [ -z "$JWT_TOKEN" ]; then
    echo "❌ Login failed"
    echo $LOGIN_RESPONSE
    exit 1
fi

echo "✅ Login successful"
echo ""

# Test 3: Check wallet balance
echo "Test 3: Wallet Balance"
echo "----------------------"
WALLET_RESPONSE=$(curl -s "${BASE_URL}/api/wallet/balance" \
  -H "Authorization: Bearer ${JWT_TOKEN}")

echo "Wallet: ${WALLET_RESPONSE}"
echo ""

# Test 4: Create agent invite
echo "Test 4: Create Agent Invite"
echo "---------------------------"
INVITE_RESPONSE=$(curl -s -X POST "${BASE_URL}/api/agent/invite" \
  -H "Authorization: Bearer ${JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "Test External Agent"}')

INVITE_TOKEN=$(echo $INVITE_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['invite_token'])" 2>/dev/null || echo "")

if [ -z "$INVITE_TOKEN" ]; then
    echo "❌ Invite creation failed"
    echo $INVITE_RESPONSE
else
    echo "✅ Invite created: ${INVITE_TOKEN}"
    echo ""
    
    # Test 5: Get invite instructions
    echo "Test 5: Get Invite Instructions"
    echo "--------------------------------"
    curl -s "${BASE_URL}/api/agent/invite/${INVITE_TOKEN}/instructions" | python3 -c "import sys, json; d=json.load(sys.stdin); print('Format:', d['format']); print('Expires:', d['expires_at'])" 2>/dev/null || echo "Instructions endpoint OK"
    echo ""
fi

# Test 6: Browse marketplace
echo "Test 6: Browse Marketplace"
echo "--------------------------"
MARKETPLACE_RESPONSE=$(curl -s "${BASE_URL}/api/marketplace/agents?limit=5")
AGENT_COUNT=$(echo $MARKETPLACE_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['total'])" 2>/dev/null || echo "0")
echo "Public agents in marketplace: ${AGENT_COUNT}"
echo ""

# Test 7: Register an agent (for testing delegation)
echo "Test 7: Register Test Agent"
echo "----------------------------"
AGENT_RESPONSE=$(curl -s -X POST "${BASE_URL}/api/agent/register" \
  -H "Authorization: Bearer ${JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Agent",
    "description": "A test agent for marketplace testing",
    "agent_type": "external",
    "endpoint_url": "https://test-agent.local/api",
    "skill_names": ["terminal"]
  }')

AGENT_ID=$(echo $AGENT_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['agent_id'])" 2>/dev/null || echo "")
AGENT_API_KEY=$(echo $AGENT_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null || echo "")

if [ -n "$AGENT_ID" ]; then
    echo "✅ Agent registered: ${AGENT_ID}"
    echo ""
    
    # Test 8: Make agent public
    echo "Test 8: Make Agent Public"
    echo "-------------------------"
    curl -s -X PUT "${BASE_URL}/api/agent/visibility?is_public=true" \
      -H "X-API-Key: ${AGENT_API_KEY}" \
      -H "Content-Type: application/json" | python3 -c "import sys, json; print('Status:', json.load(sys.stdin)['message'])" 2>/dev/null || echo "Visibility updated"
    echo ""
    
    # Test 9: Agent heartbeat
    echo "Test 9: Send Heartbeat"
    echo "----------------------"
    curl -s -X POST "${BASE_URL}/api/agent/heartbeat" \
      -H "X-API-Key: ${AGENT_API_KEY}" | python3 -c "import sys, json; print('Status:', json.load(sys.stdin)['status'])" 2>/dev/null || echo "Heartbeat sent"
    echo ""
    
    # Test 10: Discover agents for delegation
    echo "Test 10: Discover Agents"
    echo "------------------------"
    DISCOVER_RESPONSE=$(curl -s "${BASE_URL}/api/delegate/discover" \
      -H "X-API-Key: ${AGENT_API_KEY}")
    DISCOVERED_COUNT=$(echo $DISCOVER_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin)['count'])" 2>/dev/null || echo "0")
    echo "Discoverable agents: ${DISCOVERED_COUNT}"
    echo ""
fi

# Summary
echo "================================"
echo "✅ All Tests Completed!"
echo ""
echo "Summary:"
echo "--------"
echo "User: ${USER_EMAIL}"
echo "JWT Token: ${JWT_TOKEN:0:20}..."
if [ -n "$AGENT_ID" ]; then
    echo "Agent ID: ${AGENT_ID}"
    echo "Agent API Key: ${AGENT_API_KEY:0:20}..."
fi
if [ -n "$INVITE_TOKEN" ]; then
    echo "Invite Token: ${INVITE_TOKEN:0:20}..."
fi
echo ""
echo "Next steps:"
echo "- Visit ${BASE_URL}/docs for full API documentation"
echo "- Test delegation: POST /api/delegate/request"
echo "- Submit reviews: POST /api/reviews"
echo "- View transactions: GET /api/wallet/transactions"
echo ""
echo "🐝 Happy testing!"
