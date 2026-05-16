.PHONY: build up down logs clean

build:
	docker build -t load_tests . && docker-compose up -d

up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f load_tests

clean:
	docker-compose down -v
