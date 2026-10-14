from log_inspector.analysis.stack_traces import extract_stack_trace

PYTHON = """Traceback (most recent call last):
  File "/var/task/app.py", line 42, in handler
    order = load_order(event)
  File "/var/task/orders.py", line 10, in load_order
    return event["order_id"]
KeyError: 'order_id'"""

LAMBDA_PYTHON = """[ERROR] KeyError: 'order_id'
Traceback (most recent call last):
  File "/var/task/app.py", line 42, in handler
    return event["order_id"]"""

JAVA = """java.lang.RuntimeException: Checkout failed
\tat com.shop.Checkout.run(Checkout.java:88)
\tat com.shop.Main.main(Main.java:12)
Caused by: java.sql.SQLTimeoutException: Connection timed out after 30000ms
\tat org.postgresql.Driver.connect(Driver.java:301)
\tat com.shop.Db.open(Db.java:40)
\t... 2 more"""

NODE = """TypeError: Cannot read properties of undefined (reading 'id')
    at handler (/var/task/index.js:15:22)
    at Runtime.handleOnceNonStreaming (file:///var/runtime/index.mjs:1173:29)"""


def test_python_traceback():
    trace = extract_stack_trace(PYTHON)

    assert trace.exception_type == "KeyError"
    assert trace.message == "'order_id'"
    assert trace.origin == "load_order (/var/task/orders.py:10)"
    assert trace.frames == ("handler (/var/task/app.py:42)", "load_order (/var/task/orders.py:10)")


def test_lambda_python_error_header():
    trace = extract_stack_trace(LAMBDA_PYTHON)

    assert trace.exception_type == "KeyError"
    assert trace.message == "'order_id'"
    assert trace.origin == "handler (/var/task/app.py:42)"


def test_java_uses_root_cause():
    trace = extract_stack_trace(JAVA)

    assert trace.exception_type == "java.sql.SQLTimeoutException"
    assert trace.message == "Connection timed out after 30000ms"
    assert trace.origin == "org.postgresql.Driver.connect(Driver.java:301)"
    assert trace.frames[1] == "com.shop.Db.open(Db.java:40)"


def test_node_stack():
    trace = extract_stack_trace(NODE)

    assert trace.exception_type == "TypeError"
    assert trace.message.startswith("Cannot read properties of undefined")
    assert trace.origin == "handler (/var/task/index.js:15:22)"


def test_plain_lines_have_no_stack_trace():
    assert extract_stack_trace("ERROR Database connection timeout") is None
    assert extract_stack_trace("line one\nline two") is None
