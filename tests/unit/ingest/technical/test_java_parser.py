"""تست‌های java_parser.py."""

import pytest
from pathlib import Path
from src.ingest.technical.java_parser import (
    parse_java_file, join_endpoint_paths, is_test_path,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


_CONTROLLER_SRC = '''package com.example.demo;

import org.springframework.web.bind.annotation.*;
import java.util.List;

/**
 * Sample controller for testing.
 */
@RestController
@RequestMapping("/nzh/biz")
public class DemoController {

    /**
     * Lists items.
     */
    @GetMapping("/list")
    public List<String> listItems(@RequestParam String id) throws Exception {
        if (id == null) {
            return null;
        }
        return doWork(id);
    }

    private List<String> doWork(String id) {
        return null;
    }
}
'''

_TEST_SRC = '''package com.example.demo;

class DemoControllerTest {
    void testListItems() {
        DemoController c = new DemoController();
        c.listItems("x");
    }
}
'''

_INTERFACE_SRC = '''package com.example.demo;

public interface Greeter {
    String greet(String name);
}
'''

_MULTI_MAPPING_SRC = '''package com.example.demo;

import org.springframework.web.bind.annotation.*;

@RequestMapping(value = "/nzh/biz", method = RequestMethod.POST)
public class OtherController {

    @RequestMapping(value = {"/a", "/b"})
    public void multi() {}

    @Deprecated
    public void noArgs() {}
}
'''

_BROKEN_SRC = '''package com.example.broken

public class Broken {
    public void foo( {
'''

_REPOSITORY_SRC = '''package com.example.demo;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.client.RestTemplate;

public class BizRepository {

    private static final String BASE_PATH = "/nzh/biz";

    @Value("${external.biz.base-url}")
    private String baseUrl;

    private final RestTemplate restTemplate = new RestTemplate();

    public String listItems(String id) {
        String url = baseUrl + BASE_PATH + "/list?id=" + id;
        return restTemplate.getForObject(url, String.class);
    }

    public void createItem(String payload) {
        restTemplate.postForEntity(baseUrl + "/nzh/biz/create", payload, String.class);
    }

    public void noExternalCall() {
        System.out.println("nothing external here");
    }
}
'''

_FEIGN_CLIENT_SRC = '''package com.example.demo;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;

@FeignClient(name = "biz-service")
public interface BizClient {

    @GetMapping("/nzh/biz/list")
    String listItems();
}
'''


# ── join_endpoint_paths ────────────────────────────────────────────────────────

class TestJoinEndpointPaths:
    def test_joins_base_and_sub(self):
        assert join_endpoint_paths("/nzh/biz", "/list") == "/nzh/biz/list"

    def test_base_only(self):
        assert join_endpoint_paths("/nzh/biz", None) == "/nzh/biz"

    def test_sub_only(self):
        assert join_endpoint_paths(None, "/list") == "/list"

    def test_both_none_returns_none(self):
        assert join_endpoint_paths(None, None) is None

    def test_strips_extra_slashes(self):
        assert join_endpoint_paths("/nzh/biz/", "/list/") == "/nzh/biz/list"


# ── is_test_path ────────────────────────────────────────────────────────────────

class TestIsTestPath:
    def test_test_suffix_detected(self):
        assert is_test_path(Path("src/main/FooServiceTest.java"))

    def test_test_folder_detected(self):
        assert is_test_path(Path("src/test/java/com/Foo.java"))

    def test_regular_class_not_test(self):
        assert not is_test_path(Path("src/main/FooService.java"))


# ── parse_java_file — AST ─────────────────────────────────────────────────────

class TestParseJavaFileAst:
    def test_extracts_package_and_class_name(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        records = parse_java_file(path)
        assert len(records) == 1
        assert records[0]["package"] == "com.example.demo"
        assert records[0]["class_name"] == "DemoController"
        assert records[0]["qualified_name"] == "com.example.demo.DemoController"

    def test_parse_mode_is_ast(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        assert parse_java_file(path)[0]["parse_mode"] == "ast"

    def test_extracts_class_level_endpoint_path(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        assert parse_java_file(path)[0]["base_endpoint_path"] == "/nzh/biz"

    def test_extracts_method_signatures(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert set(methods) == {"listItems", "doWork"}
        assert methods["listItems"]["params"] == [{"name": "id", "type": "String"}]

    def test_method_level_endpoint_and_http_method(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["listItems"]["http_method"] == "GET"
        assert methods["listItems"]["endpoint_path"] == "/list"

    def test_non_endpoint_method_has_no_http_method(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["doWork"]["http_method"] is None

    def test_javadoc_extracted_for_class(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        assert "Sample controller" in parse_java_file(path)[0]["javadoc"]

    def test_javadoc_extracted_for_method(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert "Lists items" in methods["listItems"]["javadoc"]

    def test_method_body_captured(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert "doWork(id)" in methods["listItems"]["body"]

    def test_throws_captured(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert "Exception" in methods["listItems"]["throws"]

    def test_calls_captured(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert "doWork" in methods["listItems"]["calls"]

    def test_is_test_flag_from_class_name(self, tmp_path):
        path = _write(tmp_path, "DemoControllerTest.java", _TEST_SRC)
        assert parse_java_file(path)[0]["is_test"] is True

    def test_test_method_calls_captured_with_qualifier(self, tmp_path):
        path = _write(tmp_path, "DemoControllerTest.java", _TEST_SRC)
        methods = parse_java_file(path)[0]["methods"]
        assert "c.listItems" in methods[0]["calls"]

    def test_interface_parsed_as_interface_type(self, tmp_path):
        path = _write(tmp_path, "Greeter.java", _INTERFACE_SRC)
        record = parse_java_file(path)[0]
        assert record["class_type"] == "interface"

    def test_interface_abstract_method_has_empty_body(self, tmp_path):
        path = _write(tmp_path, "Greeter.java", _INTERFACE_SRC)
        method = parse_java_file(path)[0]["methods"][0]
        assert method["body"] == ""

    def test_named_element_value_pair_mapping(self, tmp_path):
        path = _write(tmp_path, "OtherController.java", _MULTI_MAPPING_SRC)
        record = parse_java_file(path)[0]
        assert record["base_endpoint_path"] == "/nzh/biz"

    def test_array_value_mapping_takes_first_path(self, tmp_path):
        path = _write(tmp_path, "OtherController.java", _MULTI_MAPPING_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["multi"]["endpoint_path"] == "/a"

    def test_annotation_without_args_has_no_http_mapping(self, tmp_path):
        path = _write(tmp_path, "OtherController.java", _MULTI_MAPPING_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["noArgs"]["http_method"] is None


# ── parse_java_file — regex fallback ──────────────────────────────────────────

class TestParseJavaFileRegexFallback:
    def test_broken_syntax_does_not_raise(self, tmp_path):
        path = _write(tmp_path, "Broken.java", _BROKEN_SRC)
        records = parse_java_file(path)   # نباید exception بدهد
        assert len(records) == 1

    def test_broken_syntax_uses_regex_fallback_mode(self, tmp_path):
        path = _write(tmp_path, "Broken.java", _BROKEN_SRC)
        assert parse_java_file(path)[0]["parse_mode"] == "regex_fallback"

    def test_broken_syntax_still_extracts_class_name(self, tmp_path):
        path = _write(tmp_path, "Broken.java", _BROKEN_SRC)
        assert parse_java_file(path)[0]["class_name"] == "Broken"

    def test_empty_file_does_not_raise(self, tmp_path):
        path = _write(tmp_path, "Empty.java", "")
        records = parse_java_file(path)
        assert isinstance(records, list)


# ── فراخوانی‌های خروجی به سرویس‌های خارجی ─────────────────────────────────────

class TestExternalCalls:
    def test_literal_plus_constant_concatenation_resolved(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        calls = methods["listItems"]["external_calls"]
        assert len(calls) == 1
        assert "/nzh/biz/list" in calls[0]["path_template"]

    def test_unresolved_value_field_kept_as_placeholder(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        call = methods["listItems"]["external_calls"][0]
        assert "${external.biz.base-url}" in call["path_template"]
        assert call["resolved"] is False

    def test_method_parameter_becomes_named_placeholder(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        call = methods["listItems"]["external_calls"][0]
        assert "{id}" in call["path_template"]

    def test_http_method_guessed_from_call_name(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["listItems"]["external_calls"][0]["http_method"] == "GET"
        assert methods["createItem"]["external_calls"][0]["http_method"] == "POST"

    def test_inline_concatenation_without_local_var(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        call = methods["createItem"]["external_calls"][0]
        assert "/nzh/biz/create" in call["path_template"]

    def test_method_without_outgoing_call_has_empty_list(self, tmp_path):
        path = _write(tmp_path, "BizRepository.java", _REPOSITORY_SRC)
        methods = {m["name"]: m for m in parse_java_file(path)[0]["methods"]}
        assert methods["noExternalCall"]["external_calls"] == []

    def test_regex_fallback_has_no_external_calls(self, tmp_path):
        path = _write(tmp_path, "Broken.java", _BROKEN_SRC)
        record = parse_java_file(path)[0]
        assert all(m["external_calls"] == [] for m in record["methods"])


# ── Feign client (سرویس خارجی annotation-based) ───────────────────────────────

class TestFeignClientDetection:
    def test_feign_annotated_interface_flagged(self, tmp_path):
        path = _write(tmp_path, "BizClient.java", _FEIGN_CLIENT_SRC)
        record = parse_java_file(path)[0]
        assert record["is_external_client"] is True

    def test_non_feign_class_not_flagged(self, tmp_path):
        path = _write(tmp_path, "DemoController.java", _CONTROLLER_SRC)
        assert parse_java_file(path)[0]["is_external_client"] is False

    def test_feign_method_still_gets_endpoint_path(self, tmp_path):
        path = _write(tmp_path, "BizClient.java", _FEIGN_CLIENT_SRC)
        method = parse_java_file(path)[0]["methods"][0]
        assert method["endpoint_path"] == "/nzh/biz/list"
        assert method["http_method"] == "GET"
