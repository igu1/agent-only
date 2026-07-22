# CronoCRM Agent Docker Setup

## Quick Start

1. **Build and run with Docker Compose:**
   ```bash
   cd agent
   docker-compose up --build
   ```

2. **Run in background:**
   ```bash
   docker-compose up -d --build
   ```

3. **Stop services:**
   ```bash
   docker-compose down
   ```

4. **View logs:**
   ```bash
   docker-compose logs -f agent
   ```

## Environment Setup

Create a `.env` file in the agent directory:

```env
# Database Configuration (Django-style)
DB_ENGINE=django.db.backends.postgresql
DB_NAME=crm
DB_HOST=host.docker.internal
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=eesahello123

# Other environment variables
DEBUG=false
LOG_LEVEL=info
```

**Important**: The agent connects to an external PostgreSQL database. Make sure:
- The database is accessible from the Docker container
- Network connectivity is properly configured
- Database credentials are correct

## Services

### Agent Service Only
- **Container**: crono-agent
- **Port**: 8001
- **Health Check**: http://localhost:8001/health
- **Auto-restart**: Yes
- **Database**: External PostgreSQL connection

## Development

### Build only the agent image:
```bash
docker build -t crono-agent .
```

### Run the agent container:
```bash
docker run -p 8001:8001 --env-file .env crono-agent
```

### Access the container:
```bash
docker exec -it crono-agent bash
```

## Production Considerations

1. **Use proper secrets management** for production
2. **Add resource limits** for containers
3. **Set up proper logging** and monitoring
4. **Configure network access** to external database
5. **Use secure database connections** (SSL/TLS)

## Database Connectivity

### Network Access
Ensure the external database is accessible from the Docker container:
- **Same machine**: Use `host.docker.internal` or the host IP
- **Different machine**: Use the actual IP address
- **Cloud database**: Use the provided connection string

### Example DATABASE_URL formats:
```env
# Local database
DATABASE_URL=postgresql://user:pass@host.docker.internal:5432/crono_crm

# Remote database
DATABASE_URL=postgresql://user:pass@db.example.com:5432/crono_crm

# Cloud database (AWS RDS, etc.)
DATABASE_URL=postgresql://user:pass@xxx.rds.amazonaws.com:5432/crono_crm
```

## Troubleshooting

### Check container status:
```bash
docker-compose ps
```

### View agent logs:
```bash
docker-compose logs agent
```

### Test database connection:
```bash
docker exec -it crono-agent bash
python -c "from agent.infra.runtime import get_db; print('DB connection OK')"
```

### Restart services:
```bash
docker-compose restart
```

### Clean up:
```bash
docker-compose down
```

## API Endpoints

- **Health**: http://localhost:8001/health
- **Chat**: http://localhost:8001/chat
- **Docs**: http://localhost:8001/docs (if tools route is enabled)
