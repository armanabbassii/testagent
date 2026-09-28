import sys
sys.path.insert(0, "/home/dotin/Downloads/agents2/agents")
from src.agents.test_case_generator.swagger_analyzer import (
    SwaggerAnalyzer,
    parse_swagger_fragment,
    filter_endpoints,
)

analyzer = SwaggerAnalyzer()
url = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin#/voucher-admin-controller"

spec = analyzer.analyze(url)
print("Before filter:", len(spec.endpoints))

controller, operation_id = parse_swagger_fragment(url)
print("Parsed fragment ->", controller, operation_id)

filtered = filter_endpoints(spec.endpoints, controller, operation_id)
print("After filter:", len(filtered))
for ep in filtered:
    print(" -", ep.method, ep.path, ep.tags)