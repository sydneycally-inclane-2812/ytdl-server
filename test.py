import yaml

with open("logger_config.yaml", "r") as f:
	config = yaml.safe_load(f)

print(config)