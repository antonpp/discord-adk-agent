gcloud run deploy discord-hackathon-bot \
  --source . \
  --region europe-north2 \
  --allow-unauthenticated \
  --set-env-vars ADK_APP_NAME=hackathon_support \
  --set-secrets DISCORD_API_KEY=DISCORD_API_KEY:latest,ADK_BASE_URL=ADK_BASE_URL:latest \
  --max-instances=1 \
  --min-instances=1 \
  --no-cpu-throttling
