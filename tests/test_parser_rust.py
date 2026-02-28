"""Tests for the Rust parser in src/pipeline/parser.py."""

from src.pipeline.parser import parse_file


def test_parse_rust_function():
    source = 'pub fn greet(name: &str) -> String {\n    format!("Hello, {}!", name)\n}\n'
    result = parse_file("main.rs", source)
    assert result is not None
    assert result.language == "rust"
    assert len(result.top_level_symbols) == 1
    sym = result.top_level_symbols[0]
    assert sym.name == "greet"
    assert sym.kind == "function"
    assert sym.is_exported is True
    assert "fn greet" in sym.signature


def test_parse_rust_private_function():
    source = "fn helper() -> i32 {\n    42\n}\n"
    result = parse_file("lib.rs", source)
    assert result is not None
    sym = result.top_level_symbols[0]
    assert sym.name == "helper"
    assert sym.is_exported is False


def test_parse_rust_struct():
    source = "pub struct Config {\n    pub name: String,\n    port: u16,\n}\n"
    result = parse_file("config.rs", source)
    assert result is not None
    assert len(result.top_level_symbols) == 1
    sym = result.top_level_symbols[0]
    assert sym.name == "Config"
    assert sym.kind == "class"
    assert sym.is_exported is True


def test_parse_rust_enum():
    source = "pub enum Status {\n    Active,\n    Inactive,\n    Pending(String),\n}\n"
    result = parse_file("status.rs", source)
    assert result is not None
    sym = result.top_level_symbols[0]
    assert sym.name == "Status"
    assert sym.kind == "class"


def test_parse_rust_impl_with_methods():
    source = (
        "struct Server {\n"
        "    port: u16,\n"
        "}\n"
        "\n"
        "impl Server {\n"
        "    pub fn new(port: u16) -> Self {\n"
        "        Server { port }\n"
        "    }\n"
        "\n"
        "    pub fn start(&self) {\n"
        '        println!("Starting on port {}", self.port);\n'
        "    }\n"
        "\n"
        "    fn validate(&self) -> bool {\n"
        "        self.port > 0\n"
        "    }\n"
        "}\n"
    )
    result = parse_file("server.rs", source)
    assert result is not None
    # struct + impl
    assert len(result.top_level_symbols) == 2

    # Find the impl block
    impl_sym = next(s for s in result.top_level_symbols if len(s.children) > 0)
    assert impl_sym.name == "Server"
    assert len(impl_sym.children) == 3

    new_method = impl_sym.children[0]
    assert new_method.name == "new"
    assert new_method.kind == "method"
    assert new_method.parent_name == "Server"
    assert new_method.is_exported is True

    validate_method = impl_sym.children[2]
    assert validate_method.name == "validate"
    assert validate_method.is_exported is False


def test_parse_rust_trait():
    source = (
        "pub trait Handler {\n"
        "    fn handle(&self, req: Request) -> Response;\n"
        "    fn name(&self) -> &str;\n"
        "}\n"
    )
    result = parse_file("handler.rs", source)
    assert result is not None
    sym = result.top_level_symbols[0]
    assert sym.name == "Handler"
    assert sym.kind == "class"
    assert sym.is_exported is True


def test_parse_rust_use_declarations():
    source = (
        "use std::io;\n"
        "use std::collections::HashMap;\n"
        "use crate::config::Settings;\n"
        "\n"
        "fn main() {\n"
        '    println!("hello");\n'
        "}\n"
    )
    result = parse_file("main.rs", source)
    assert result is not None
    assert len(result.imports) == 3
    assert "use std::io" in result.imports[0]
    assert len(result.top_level_symbols) == 1


def test_parse_rust_trait_impl():
    source = (
        "struct MyHandler;\n"
        "\n"
        "impl Handler for MyHandler {\n"
        "    fn handle(&self, req: Request) -> Response {\n"
        "        Response::ok()\n"
        "    }\n"
        "}\n"
    )
    result = parse_file("my_handler.rs", source)
    assert result is not None
    # struct + impl
    assert len(result.top_level_symbols) == 2
    impl_sym = next(s for s in result.top_level_symbols if len(s.children) > 0)
    assert impl_sym.name == "MyHandler"
    assert len(impl_sym.children) == 1
    assert impl_sym.children[0].name == "handle"


def test_parse_rust_empty_file():
    result = parse_file("empty.rs", "")
    assert result is not None
    assert result.language == "rust"
    assert len(result.top_level_symbols) == 0
    assert len(result.imports) == 0
