# ========================================
# Chronicle Management System
# ========================================
# Central management interface for Chronicle project
# Handles configuration, deployment, and maintenance tasks

# Load environment variables from .env file
ifneq (,$(wildcard ./.env))
    include .env
    export $(shell sed 's/=.*//' .env | grep -v '^\s*$$' | grep -v '^\s*\#')
endif

# Load configuration definitions
include config.env
# Export all variables from config.env
export $(shell sed 's/=.*//' config.env | grep -v '^\s*$$' | grep -v '^\s*\#')

# Script directories
SCRIPTS_DIR := scripts
K8S_SCRIPTS_DIR := $(SCRIPTS_DIR)/k8s

.PHONY: help menu setup-k8s setup-infrastructure setup-rbac setup-storage-pvc config config-docker config-k8s config-all clean deploy deploy-docker deploy-k8s deploy-k8s-full deploy-speaker deploy-infrastructure deploy-apps check-infrastructure check-apps build-backend up-backend down-backend k8s-status k8s-cleanup k8s-purge audio-manage test-robot test-robot-integration test-robot-unit test-robot-endpoints test-robot-specific test-robot-clean

# Default target
.DEFAULT_GOAL := menu

menu: ## Show interactive menu (default)
	@echo "🎯 Chronicle Management System"
	@echo "================================"
	@echo
	@echo "📋 Quick Actions:"
	@echo "  setup-dev          🛠️  Setup development environment (git hooks, pre-commit)"
	@echo "  setup-k8s          🏗️  Complete Kubernetes setup (registry + infrastructure + RBAC)"
	@echo "  config             📝 Generate all configuration files"
	@echo "  deploy             🚀 Deploy using configured mode ($(DEPLOYMENT_MODE))"
	@echo "  k8s-status         📊 Check Kubernetes cluster status"
	@echo "  k8s-cleanup        🧹 Clean up Kubernetes resources"
	@echo "  audio-manage       🎵 Manage audio files"
	@echo
	@echo "🧪 Testing:"
	@echo "  test-robot         🧪 Run all Robot Framework tests"
	@echo "  test-robot-integration 🔬 Run integration tests only"
	@echo "  test-robot-endpoints 🌐 Run endpoint tests only"
	@echo
	@echo "📝 Configuration:"
	@echo "  config-docker      🐳 Generate Docker Compose .env files"
	@echo "  config-k8s         ☸️  Generate Kubernetes files (Skaffold env + ConfigMap/Secret)"
	@echo
	@echo "🚀 Deployment:"
	@echo "  deploy-docker      🐳 Deploy with Docker Compose"
	@echo "  deploy-k8s         ☸️  Deploy to Kubernetes with Skaffold"
	@echo "  deploy-k8s-full    🏗️  Deploy infrastructure + applications"
	@echo "  deploy-speaker     🗣️  Build + deploy speaker recognition (GPU, speech ns)"
	@echo
	@echo "🔧 Utilities:"
	@echo "  k8s-purge          🗑️  Purge unused images (registry + container)"
	@echo "  check-infrastructure 🔍 Check infrastructure services"
	@echo "  check-apps         🔍 Check application services"
	@echo "  clean              🧹 Clean up generated files"
	@echo
	@echo "Current configuration:"
	@echo "  DOMAIN: $(DOMAIN)"
	@echo "  DEPLOYMENT_MODE: $(DEPLOYMENT_MODE)"
	@echo "  CONTAINER_REGISTRY: $(CONTAINER_REGISTRY)"
	@echo "  SPEAKER_NODE: $(SPEAKER_NODE)"
	@echo "  INFRASTRUCTURE_NAMESPACE: $(INFRASTRUCTURE_NAMESPACE)"
	@echo "  APPLICATION_NAMESPACE: $(APPLICATION_NAMESPACE)"
	@echo
	@echo "💡 Tip: Run 'make help' for detailed help on any target"

help: ## Show detailed help for all targets
	@echo "🎯 Chronicle Management System - Detailed Help"
	@echo "================================================"
	@echo
	@echo "🏗️  KUBERNETES SETUP:"
	@echo "  setup-k8s          Complete initial Kubernetes setup"
	@echo "                     - Configures insecure registry access"
	@echo "                     - Sets up infrastructure services (MongoDB, FalkorDB)"
	@echo "                     - Creates shared models PVC"
	@echo "                     - Sets up cross-namespace RBAC"
	@echo "                     - Generates and applies configuration"
	@echo "  setup-infrastructure Deploy infrastructure services (MongoDB, FalkorDB)"
	@echo "  setup-rbac         Set up cross-namespace RBAC"
	@echo "  setup-storage-pvc  Create shared models PVC"
	@echo
	@echo "📝 CONFIGURATION:"
	@echo "  config             Generate all configuration files (Docker + K8s)"
	@echo "  config-docker      Generate Docker Compose .env files"
	@echo "  config-k8s         Generate Kubernetes files (Skaffold env + ConfigMap/Secret)"
	@echo
	@echo "🚀 DEPLOYMENT:"
	@echo "  deploy             Deploy using configured deployment mode"
	@echo "  deploy-docker      Deploy with Docker Compose"
	@echo "  deploy-k8s         Deploy to Kubernetes with Skaffold"
	@echo "  deploy-k8s-full    Deploy infrastructure + applications"
	@echo "  deploy-speaker     Build + deploy speaker recognition to the speech namespace"
	@echo
	@echo "🔧 KUBERNETES UTILITIES:"
	@echo "  k8s-status         Check Kubernetes cluster status and health"
	@echo "  k8s-cleanup        Clean up Kubernetes resources and storage"
	@echo "  k8s-purge          Purge unused images (registry + container)"
	@echo
	@echo "🎵 AUDIO MANAGEMENT:"
	@echo "  audio-manage       Interactive audio file management"
	@echo
	@echo "🧪 ROBOT FRAMEWORK TESTING:"
	@echo "  test-robot         Run all Robot Framework tests"
	@echo "  test-robot-integration Run integration tests only"
	@echo "  test-robot-endpoints Run endpoint tests only"
	@echo "  test-robot-specific FILE=path Run specific test file"
	@echo "  test-robot-clean   Clean up test results"
	@echo
	@echo "🔍 MONITORING:"
	@echo "  check-infrastructure Check if infrastructure services are running"
	@echo "  check-apps         Check if application services are running"
	@echo
	@echo "🧹 CLEANUP:"
	@echo "  clean              Clean up generated configuration files"

# ========================================
# DEVELOPMENT SETUP
# ========================================

setup-dev: ## Setup development environment (git hooks, pre-commit)
	@echo "🛠️  Setting up development environment..."
	@echo ""
	@bash scripts/check_uv.sh
	@echo "📦 Installing pre-commit..."
	@uv tool install pre-commit
	@echo ""
	@echo "🔧 Installing git hooks..."
	@pre-commit install --hook-type pre-push
	@pre-commit install --hook-type pre-commit
	@echo ""
	@echo "✅ Development environment setup complete!"
	@echo ""
	@echo "💡 Hooks installed:"
	@echo "  • Robot Framework tests run before push"
	@echo "  • Black/isort format Python code on commit"
	@echo "  • Code quality checks on commit"
	@echo ""
	@echo "⚙️  To skip hooks: git push --no-verify / git commit --no-verify"

# ========================================
# KUBERNETES SETUP
# ========================================

setup-k8s: ## Initial Kubernetes setup (registry + infrastructure)
	@echo "🏗️  Starting Kubernetes initial setup..."
	@echo "This will set up the complete infrastructure for Chronicle"
	@echo
	@echo "📋 Setup includes:"
	@echo "  • Insecure registry configuration"
	@echo "  • Infrastructure services (MongoDB, FalkorDB)"
	@echo "  • Shared models PVC for speaker recognition"
	@echo "  • Cross-namespace RBAC"
	@echo "  • Configuration generation and application"
	@echo
	@read -p "Enter your Kubernetes node IP address: " node_ip; \
	if [ -z "$$node_ip" ]; then \
		echo "❌ Node IP is required"; \
		exit 1; \
	fi; \
	echo "🔧 Step 1: Configuring insecure registry access on $$node_ip..."; \
	$(SCRIPTS_DIR)/configure-insecure-registry-remote.sh $$node_ip; \
	echo "📦 Step 2: Setting up storage for speaker recognition..."; \
	$(K8S_SCRIPTS_DIR)/setup-storage.sh; \
	echo "📝 Step 3: Generating configuration files..."; \
	$(MAKE) config-k8s; \
	echo "🏗️  Step 4: Setting up infrastructure services..."; \
	$(MAKE) setup-infrastructure; \
	echo "🔐 Step 5: Setting up cross-namespace RBAC..."; \
	$(MAKE) setup-rbac; \
	echo "💾 Step 6: Creating shared models PVC..."; \
	$(MAKE) setup-storage-pvc; \
	echo "✅ Kubernetes initial setup completed!"
	@echo
	@echo "🎯 Next steps:"
	@echo "  • Run 'make deploy' to deploy applications"
	@echo "  • Run 'make k8s-status' to check cluster status"
	@echo "  • Run 'make help' for more options"

setup-infrastructure: ## Set up infrastructure services (MongoDB, FalkorDB)
	@echo "🏗️  Setting up infrastructure services..."
	@echo "Deploying MongoDB and FalkorDB to $(INFRASTRUCTURE_NAMESPACE) namespace..."
	@set -a; source skaffold.env; set +a; skaffold run --profile=infrastructure --default-repo=$(CONTAINER_REGISTRY)
	@echo "⏳ Waiting for infrastructure services to be ready..."
	@kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=mongodb -n $(INFRASTRUCTURE_NAMESPACE) --timeout=300s || echo "⚠️  MongoDB not ready yet"
	@echo "✅ Infrastructure services deployed"

setup-rbac: ## Set up cross-namespace RBAC
	@echo "🔐 Setting up cross-namespace RBAC..."
	@kubectl apply -f k8s-manifests/cross-namespace-rbac.yaml
	@echo "✅ Cross-namespace RBAC configured"

setup-storage-pvc: ## Set up shared models PVC
	@echo "💾 Setting up shared models PVC..."
	@kubectl apply -f k8s-manifests/shared-models-pvc.yaml
	@echo "⏳ Waiting for PVC to be bound..."
	@kubectl wait --for=condition=bound pvc/shared-models-cache -n speech --timeout=60s || echo "⚠️  PVC not bound yet"
	@echo "✅ Shared models PVC created"

# ========================================
# CONFIGURATION
# ========================================

config: config-all ## Generate all configuration files

config-docker: ## Generate Docker Compose configuration files
	@echo "🐳 Generating Docker Compose configuration files..."
	@CONFIG_FILE=config.env.dev uv run --with python-dotenv python scripts/generate-docker-configs.py
	@echo "✅ Docker Compose configuration files generated"

config-k8s: ## Generate Kubernetes configuration files (ConfigMap/Secret only - no .env files)
	@echo "☸️  Generating Kubernetes configuration files..."
	@uv run --with python-dotenv python scripts/generate-k8s-configs.py
	@echo "📦 Applying ConfigMap and Secret to Kubernetes..."
	@kubectl apply -f k8s-manifests/configmap.yaml -n $(APPLICATION_NAMESPACE) 2>/dev/null || echo "⚠️  ConfigMap not applied (cluster not available?)"
	@kubectl apply -f k8s-manifests/secrets.yaml -n $(APPLICATION_NAMESPACE) 2>/dev/null || echo "⚠️  Secret not applied (cluster not available?)"
	@echo "📦 Copying ConfigMap and Secret to speech namespace..."
	@kubectl get configmap chronicle-config -n $(APPLICATION_NAMESPACE) -o yaml | \
		sed -e '/namespace:/d' -e '/resourceVersion:/d' -e '/uid:/d' -e '/creationTimestamp:/d' | \
		kubectl apply -n speech -f - 2>/dev/null || echo "⚠️  ConfigMap not copied to speech namespace"
	@kubectl get secret chronicle-secrets -n $(APPLICATION_NAMESPACE) -o yaml | \
		sed -e '/namespace:/d' -e '/resourceVersion:/d' -e '/uid:/d' -e '/creationTimestamp:/d' | \
		kubectl apply -n speech -f - 2>/dev/null || echo "⚠️  Secret not copied to speech namespace"
	@echo "✅ Kubernetes configuration files generated"

config-all: config-docker config-k8s ## Generate all configuration files
	@echo "✅ All configuration files generated"

clean: ## Clean up generated configuration files
	@echo "🧹 Cleaning up generated configuration files..."
	@rm -f backend/.env
	@rm -f extras/speaker-recognition/.env
	@rm -f extras/openmemory-mcp/.env
	@rm -f extras/asr-services/.env
	@rm -f extras/havpe-relay/.env
	@rm -f backends/simple/.env
	@rm -f backends/other-backends/omi-webhook-compatible/.env
	@rm -f skaffold.env
	@rm -f backends/charts/backend/templates/env-configmap.yaml
	@echo "✅ Generated files cleaned"

# ========================================
# DEPLOYMENT TARGETS
# ========================================

deploy: ## Deploy using configured deployment mode
	@echo "🚀 Deploying using $(DEPLOYMENT_MODE) mode..."
ifeq ($(DEPLOYMENT_MODE),docker-compose)
	@$(MAKE) deploy-docker
else ifeq ($(DEPLOYMENT_MODE),kubernetes)
	@$(MAKE) deploy-k8s
else
	@echo "❌ Unknown deployment mode: $(DEPLOYMENT_MODE)"
	@exit 1
endif

deploy-docker: config-docker ## Deploy using Docker Compose
	@echo "🐳 Deploying with Docker Compose..."
	@cd backend && docker-compose up -d
	@echo "✅ Docker Compose deployment completed"

deploy-k8s: config-k8s ## Deploy to Kubernetes using Skaffold
	@echo "☸️  Deploying to Kubernetes with Skaffold..."
	@set -a; source skaffold.env; set +a; skaffold run --profile=backend --default-repo=$(CONTAINER_REGISTRY)
	@echo "✅ Kubernetes deployment completed"

deploy-speaker: config-k8s ## Build (amd64, via BUILDX_BUILDER) and deploy speaker recognition
	@echo "🗣️  Deploying speaker recognition to the speech namespace..."
	@set -a; source skaffold.env; set +a; skaffold run --profile=speaker-recognition --default-repo=$(CONTAINER_REGISTRY)
	@echo "✅ Speaker recognition deployed (backend URL: $(SPEAKER_SERVICE_URL))"

deploy-k8s-full: deploy-infrastructure deploy-apps ## Deploy infrastructure + applications to Kubernetes
	@echo "✅ Full Kubernetes deployment completed"

deploy-infrastructure: ## Deploy infrastructure services to Kubernetes
	@echo "🏗️  Deploying infrastructure services..."
	@kubectl apply -f k8s-manifests/
	@echo "✅ Infrastructure deployment completed"

deploy-apps: config-k8s ## Deploy application services to Kubernetes
	@echo "📱 Deploying application services..."
	@set -a; source skaffold.env; set +a; skaffold run --profile=backend --default-repo=$(CONTAINER_REGISTRY)
	@echo "✅ Application deployment completed"

# ========================================
# UTILITY TARGETS
# ========================================

check-infrastructure: ## Check if infrastructure services are running
	@echo "🔍 Checking infrastructure services..."
	@kubectl get pods -n $(INFRASTRUCTURE_NAMESPACE) || echo "❌ Infrastructure namespace not found"
	@kubectl get services -n $(INFRASTRUCTURE_NAMESPACE) || echo "❌ Infrastructure services not found"

check-apps: ## Check if application services are running
	@echo "🔍 Checking application services..."
	@kubectl get pods -n $(APPLICATION_NAMESPACE) || echo "❌ Application namespace not found"
	@kubectl get services -n $(APPLICATION_NAMESPACE) || echo "❌ Application services not found"

# ========================================
# DEVELOPMENT TARGETS
# ========================================

build-backend: ## Build backend Docker image
	@echo "🔨 Building backend Docker image..."
	@cd backend && docker build -t backend:latest .

up-backend: config-docker ## Start backend services
	@echo "🚀 Starting backend services..."
	@cd backend && docker-compose up -d

down-backend: ## Stop backend services
	@echo "🛑 Stopping backend services..."
	@cd backend && docker-compose down

# ========================================
# KUBERNETES UTILITIES
# ========================================

k8s-status: ## Check Kubernetes cluster status and health
	@echo "📊 Checking Kubernetes cluster status..."
	@$(K8S_SCRIPTS_DIR)/cluster-status.sh

k8s-cleanup: ## Clean up Kubernetes resources and storage
	@echo "🧹 Starting Kubernetes cleanup..."
	@echo "This will help clean up registry storage and unused resources"
	@$(K8S_SCRIPTS_DIR)/cleanup-registry-storage.sh

k8s-purge: ## Purge unused images (registry + container)
	@echo "🗑️  Purging unused images..."
	@$(K8S_SCRIPTS_DIR)/purge-images.sh

# ========================================
# AUDIO MANAGEMENT
# ========================================

audio-manage: ## Interactive audio file management
	@echo "🎵 Starting audio file management..."
	@$(SCRIPTS_DIR)/manage-audio-files.sh

# ========================================
# TESTING TARGETS
# ========================================

# Define test environment variables
TEST_ENV := BACKEND_URL=http://localhost:8001 ADMIN_EMAIL=test-admin@example.com ADMIN_PASSWORD=test-admin-password-123

test-robot: ## Run all Robot Framework tests
	@echo "🧪 Running all Robot Framework tests..."
	@cd tests && $(TEST_ENV) robot --outputdir ../results .
	@echo "✅ All Robot Framework tests completed"
	@echo "📊 Results available in: results/"

test-robot-integration: ## Run integration tests only
	@echo "🧪 Running Robot Framework integration tests..."
	@cd tests && $(TEST_ENV) robot --outputdir ../results integration/
	@echo "✅ Robot Framework integration tests completed"
	@echo "📊 Results available in: results/"

test-robot-unit: ## Run unit tests only
	@echo "🧪 Running Robot Framework unit tests..."
	@cd tests && $(TEST_ENV) robot --outputdir ../results unit/ || echo "⚠️  No unit tests directory found"
	@echo "✅ Robot Framework unit tests completed"
	@echo "📊 Results available in: results/"

test-robot-endpoints: ## Run endpoint tests only
	@echo "🧪 Running Robot Framework endpoint tests..."
	@cd tests && $(TEST_ENV) robot --outputdir ../results endpoints/
	@echo "✅ Robot Framework endpoint tests completed"
	@echo "📊 Results available in: results/"

test-robot-specific: ## Run specific Robot Framework test file (usage: make test-robot-specific FILE=path/to/test.robot)
	@echo "🧪 Running specific Robot Framework test: $(FILE)"
	@if [ -z "$(FILE)" ]; then \
		echo "❌ FILE parameter is required. Usage: make test-robot-specific FILE=path/to/test.robot"; \
		exit 1; \
	fi
	@cd tests && $(TEST_ENV) robot --outputdir ../results $(FILE)
	@echo "✅ Robot Framework test completed: $(FILE)"
	@echo "📊 Results available in: results/"

test-robot-clean: ## Clean up Robot Framework test results
	@echo "🧹 Cleaning up Robot Framework test results..."
	@rm -rf results/
	@echo "✅ Test results cleaned"
