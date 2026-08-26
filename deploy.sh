#!/bin/bash

# Guru AI RAG System Deployment Script
# This script sets up and deploys the complete Guru AI system

set -e

echo "🚀 Starting Guru AI RAG System Deployment"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PROJECT_DIR="/home/ubuntu/guru_rag"
VENV_DIR="$PROJECT_DIR/venv"
LOGS_DIR="$PROJECT_DIR/logs"

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as ubuntu user
if [ "$USER" != "ubuntu" ]; then
    print_error "This script must be run as the ubuntu user"
    exit 1
fi

# Navigate to project directory
cd "$PROJECT_DIR" || {
    print_error "Cannot access project directory: $PROJECT_DIR"
    exit 1
}

print_status "Working in directory: $(pwd)"

# Create logs directory
print_status "Creating logs directory..."
mkdir -p "$LOGS_DIR"

# Activate virtual environment
print_status "Activating Python virtual environment..."
source "$VENV_DIR/bin/activate"

# Install/update dependencies
print_status "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Test OpenSearch connection
print_status "Testing OpenSearch connection..."
python3 -c "
from opensearchpy import OpenSearch
client = OpenSearch(hosts=[{'host': 'vpc-guru-rag-w7f4dc2djtwacgeelhfpd7u7li.us-east-1.es.amazonaws.com', 'port': 443}], http_auth=('admin', '@Gur#Ur@g25'), use_ssl=True, verify_certs=True)
info = client.info()
print('OpenSearch connected successfully')
print('Version:', info['version']['number'])
"

# Test embeddings
print_status "Testing OpenAI embeddings..."
python3 -c "
from openai import OpenAI
import os
from dotenv import load_dotenv
load_dotenv()
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
response = client.embeddings.create(model='text-embedding-3-large', input='test')
print(f'Embeddings working. Dimension: {len(response.data[0].embedding)}')
"

# Stop any existing services
print_status "Stopping existing services..."
sudo systemctl stop guru-rag 2>/dev/null || true
pm2 stop guru-rag-api 2>/dev/null || true
pkill -f "uvicorn.*main:app" 2>/dev/null || true

# Install PM2 globally if not installed
if ! command -v pm2 &> /dev/null; then
    print_status "Installing PM2..."
    sudo npm install -g pm2
fi

# Copy systemd service file
print_status "Setting up systemd service..."
sudo cp "$PROJECT_DIR/guru-rag.service" /etc/systemd/system/
sudo systemctl daemon-reload

# Start with PM2 (recommended for Node.js ecosystem, but can work with Python)
print_status "Starting Guru API with PM2..."
pm2 start ecosystem.config.js --env production

# Alternative: Start with systemd
# print_status "Starting Guru API with systemd..."
# sudo systemctl enable guru-rag
# sudo systemctl start guru-rag

# Wait for service to start
print_status "Waiting for service to start..."
sleep 5

# Test the API
print_status "Testing API endpoints..."
if curl -s -f http://localhost:8000/health > /dev/null; then
    print_success "Health check passed!"
else
    print_error "Health check failed!"
    exit 1
fi

# Test ask endpoint
print_status "Testing ask endpoint..."
response=$(curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What is LASIK?", "top_k": 3}')

if echo "$response" | grep -q "answer"; then
    print_success "Ask endpoint working!"
else
    print_error "Ask endpoint failed!"
    exit 1
fi

# Setup PM2 startup
print_status "Setting up PM2 auto-startup..."
sudo env PATH=$PATH:/usr/bin /usr/lib/node_modules/pm2/bin/pm2 startup systemd -u ubuntu --hp /home/ubuntu
pm2 save

# Create frontend directory structure
print_status "Setting up frontend files..."
mkdir -p "$PROJECT_DIR/frontend"
chmod +x "$PROJECT_DIR/frontend"

# Create Nginx configuration (optional)
print_status "Creating Nginx configuration..."
sudo tee /etc/nginx/sites-available/guru-rag > /dev/null <<EOF
server {
    listen 80;
    server_name _;

    # Frontend static files
    location /chat/ {
        alias $PROJECT_DIR/frontend/;
        try_files \$uri \$uri/ /chat/chat-widget.html;
        add_header Cache-Control "no-cache";
    }

    # API proxy
    location /api/ {
        proxy_pass http://localhost:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Main API
    location / {
        proxy_pass http://localhost:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Enable Nginx site
sudo ln -sf /etc/nginx/sites-available/guru-rag /etc/nginx/sites-enabled/ 2>/dev/null || true
sudo nginx -t && sudo systemctl reload nginx

print_success "🎉 Guru AI RAG System deployed successfully!"
echo ""
echo "📋 Deployment Summary:"
echo "   • API Server: http://localhost:8000"
echo "   • Health Check: http://localhost:8000/health"
echo "   • Chat Widget: http://your-server/chat/chat-widget.html"
echo "   • Embed Script: http://your-server/chat/guru-chat-embed.js"
echo "   • Example Page: http://your-server/chat/example-page.html"
echo ""
echo "🔧 Management Commands:"
echo "   • View logs: pm2 logs guru-rag-api"
echo "   • Restart: pm2 restart guru-rag-api"
echo "   • Stop: pm2 stop guru-rag-api"
echo "   • Monitor: pm2 monit"
echo ""
echo "📁 Project Structure:"
echo "   $PROJECT_DIR/"
echo "   ├── main.py              # FastAPI server"
echo "   ├── utils.py             # OpenSearch and embedding utilities"
echo "   ├── llm_providers.py     # LLM integration with fallback"
echo "   ├── ingest.py            # Document ingestion script"
echo "   ├── .env                 # Environment variables"
echo "   ├── logs/                # Application logs"
echo "   └── frontend/            # Chat widget files"
echo ""
print_warning "⚠️  Remember to:"
echo "   • Update API URLs in frontend files for production"
echo "   • Configure proper CORS settings"
echo "   • Set up SSL certificates for HTTPS"
echo "   • Configure firewall rules"
echo "   • Set up monitoring and alerts"
