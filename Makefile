.PHONY: help build up down logs restart clean

help:
	@echo "Usage: make [TARGET]"
	@echo ""
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:
	docker-compose build

up:
	docker-compose up

up-d:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

logs-agent:
	docker-compose logs -f agent

restart:
	docker-compose restart

clean:
	docker-compose down -v

shell:
	docker exec -it crono-agent bash

status:
	docker-compose ps

dev: up-d

stop: down
