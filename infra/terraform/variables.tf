variable "region" {
  type    = string
  default = "oregon"
}

variable "app_plan" {
  description = "Instance type for the app. starter avoids free-tier sleep and the shared 750 free hours."
  type        = string
  default     = "starter"
}

variable "db_plan" {
  description = "Postgres plan: free (expires after 30 days) or e.g. basic_256mb."
  type        = string
  default     = "basic_256mb"
}

variable "preview_plan" {
  description = "Instance type for the preview services the app creates."
  type        = string
  default     = "free"
}

variable "repo_url" {
  type    = string
  default = "https://github.com/jaydeep-99o/paytoprovision"
}

variable "app_base_url" {
  description = "Public URL of the app, used for PayPal return URLs."
  type        = string
  default     = "https://paytoprovision.onrender.com"
}

variable "render_api_key" {
  description = "Render API key the app uses to create/delete previews."
  type        = string
  sensitive   = true
}

variable "render_owner_id" {
  type = string
}

variable "github_token" {
  type      = string
  sensitive = true
}

variable "nebius_api_key" {
  type      = string
  sensitive = true
}

variable "nebius_model" {
  type    = string
  default = "nvidia/nemotron-3-super-120b-a12b"
}

variable "paypal_client_id" {
  type = string
}

variable "paypal_client_secret" {
  type      = string
  sensitive = true
}

variable "paypal_webhook_id" {
  type    = string
  default = ""
}

variable "allowed_repos" {
  description = "Comma-separated owner/name repos that may be listed."
  type        = string
  default     = ""
}
