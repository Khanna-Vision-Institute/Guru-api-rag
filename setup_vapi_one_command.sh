#!/bin/bash
# One-command Vapi setup for Guru AI

echo "🚀 Setting up Vapi integration..."

# Add API key to .env if not exists
if ! grep -q "VAPI_API_KEY=" /home/ubuntu/guru_rag/.env 2>/dev/null; then
    echo "VAPI_API_KEY=0f5bd46c-6de9-42a1-a75c-043bd39a3fec" >> /home/ubuntu/guru_rag/.env
    echo "✅ Added VAPI_API_KEY to .env"
fi

if ! grep -q "BACKEND_API_URL=" /home/ubuntu/guru_rag/.env 2>/dev/null; then
    echo "BACKEND_API_URL=http://ec2-100-28-122-42.compute-1.amazonaws.com:8000" >> /home/ubuntu/guru_rag/.env
    echo "✅ Added BACKEND_API_URL to .env"
fi

if ! grep -q "WEBHOOK_URL=" /home/ubuntu/guru_rag/.env 2>/dev/null; then
    echo "WEBHOOK_URL=http://ec2-100-28-122-42.compute-1.amazonaws.com:8000/vapi/webhook" >> /home/ubuntu/guru_rag/.env
    echo "✅ Added WEBHOOK_URL to .env"
fi

cd /home/ubuntu/guru_rag
source venv/bin/activate

# Install Node.js if needed
if ! command -v node &> /dev/null; then
    echo "📦 Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# Install npm packages
echo "📦 Installing npm dependencies..."
npm install axios dotenv 2>/dev/null || echo "⚠️  npm install failed, continuing..."

# Run setup
echo "🔧 Creating Vapi assistant..."
node vapi_setup.js

echo ""
echo "✅ Setup complete! Check output above for Assistant ID."
