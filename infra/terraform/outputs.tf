output "app_url" {
  value = render_web_service.app.url
}

output "service_id" {
  value = render_web_service.app.id
}

output "database_id" {
  value = render_postgres.db.id
}
