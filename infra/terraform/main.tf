# PayToProvision on Render: the app (web service) + its Postgres database.
#
# The live demo was created from render.yaml (a Render Blueprint). This is the same stack
# as Terraform, for teams that manage Render with IaC. To adopt it for an existing stack,
# `terraform import` the service and database first (see README). Preview environments are
# NOT managed here: the app creates and deletes them through the Render API at runtime.

terraform {
  required_version = ">= 1.5"
  required_providers {
    render = {
      source  = "render-oss/render"
      version = ">= 1.0"
    }
  }
}

# Reads RENDER_API_KEY and RENDER_OWNER_ID from the environment.
provider "render" {}

resource "render_postgres" "db" {
  name          = "paytoprovision-db"
  plan          = var.db_plan
  region        = var.region
  version       = "17"
  database_name = "paytoprovision"
  database_user = "paytoprovision"
  # No ip_allow_list: reachable only over Render's private network.
}

resource "render_web_service" "app" {
  name              = "paytoprovision"
  plan              = var.app_plan
  region            = var.region
  start_command     = "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips=\"*\""
  health_check_path = "/healthz"

  runtime_source = {
    native_runtime = {
      repo_url      = var.repo_url
      branch        = "main"
      runtime       = "python"
      build_command = "pip install -r requirements.txt && pip install --no-deps paypal-agent-toolkit==1.11.0"
      auto_deploy   = true
    }
  }

  env_vars = {
    PYTHON_VERSION      = { value = "3.12.10" }
    APP_ENV             = { value = "production" }
    APP_BASE_URL        = { value = var.app_base_url }
    DATABASE_URL        = { value = render_postgres.db.connection_info.internal_connection_string }
    SECRET_KEY          = { generate_value = true }
    ADMIN_TOKEN         = { generate_value = true }
    INTERNAL_REAP_TOKEN = { generate_value = true }

    RENDER_API_KEY      = { value = var.render_api_key }
    RENDER_OWNER_ID     = { value = var.render_owner_id }
    PREVIEW_NAME_PREFIX = { value = "ptp-" }
    PREVIEW_PLAN        = { value = var.preview_plan }

    GITHUB_TOKEN   = { value = var.github_token }
    NEBIUS_API_KEY = { value = var.nebius_api_key }
    NEBIUS_MODEL   = { value = var.nebius_model }

    PAYPAL_CLIENT_ID     = { value = var.paypal_client_id }
    PAYPAL_CLIENT_SECRET = { value = var.paypal_client_secret }
    PAYPAL_WEBHOOK_ID    = { value = var.paypal_webhook_id }
    ALLOWED_REPOS        = { value = var.allowed_repos }
  }
}
