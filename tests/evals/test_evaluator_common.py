from __future__ import annotations as _annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from inline_snapshot import snapshot
from pytest_mock import MockerFixture

from pydantic_ai.settings import ModelSettings

from ..conftest import try_import

with try_import() as imports_successful:
    import logfire
    from logfire.testing import CaptureLogfire

    from pydantic_evals.evaluators import EvaluationReason, EvaluatorContext
    from pydantic_evals.evaluators.common import (
        DEFAULT_EVALUATORS,
        Contains,
        Equals,
        EqualsExpected,
        HasMatchingSpan,
        IsInstance,
        LLMJudge,
        MaxDuration,
        Python,
    )
    from pydantic_evals.otel._context_in_memory_span_exporter import context_subtree
    from pydantic_evals.otel._errors import SpanTreeRecordingError
    from pydantic_evals.otel.span_tree import SpanQuery

pytestmark = [pytest.mark.skipif(not imports_successful(), reason='pydantic-evals not installed'), pytest.mark.anyio]


if TYPE_CHECKING or imports_successful():

    class MockContext(EvaluatorContext[Any, Any, Any]):
        def __init__(self, output: Any = None, expected_output: Any = None, inputs: Any = None, duration: float = 0.0):
            self.output = output
            self.expected_output = expected_output
            self.inputs = inputs
            self.duration = duration
else:
    MockContext = object


async def test_equals():
    """Test Equals evaluator."""
    evaluator = Equals(value=42)

    # Test equal values
    assert evaluator.evaluate(MockContext(output=42)) is True

    # Test unequal values
    assert evaluator.evaluate(MockContext(output=43)) is False


async def test_equals_expected():
    """Test EqualsExpected evaluator."""
    evaluator = EqualsExpected()

    # Test with matching output and expected output
    assert evaluator.evaluate(MockContext(output=42, expected_output=42)) is True

    # Test with non-matching output and expected output
    assert evaluator.evaluate(MockContext(output=42, expected_output=43)) is False

    # Test with no expected output
    assert evaluator.evaluate(MockContext(output=42, expected_output=None)) == {}


async def test_contains_string():
    """Test Contains evaluator with strings."""
    evaluator = Contains(value='test')

    # Test string containment
    assert evaluator.evaluate(MockContext(output='this is a test')).value is True

    # Test string non-containment
    assert evaluator.evaluate(MockContext(output='no match')) == snapshot(
        EvaluationReason(value=False, reason="Output string 'no match' does not contain expected string 'test'")
    )

    # Test case sensitivity
    evaluator_case_insensitive = Contains(value='TEST', case_sensitive=False)
    assert evaluator_case_insensitive.evaluate(MockContext(output='this is a test')) == snapshot(
        EvaluationReason(value=True)
    )


async def test_contains_dict():
    """Test Contains evaluator with dictionaries."""
    evaluator = Contains(value={'key': 'value'})

    # Test dictionary containment
    assert evaluator.evaluate(MockContext(output={'key': 'value', 'extra': 'data'})) == snapshot(
        EvaluationReason(value=True)
    )

    # Test dictionary key missing
    assert evaluator.evaluate(MockContext(output={'different': 'value'})) == snapshot(
        EvaluationReason(value=False, reason="Output dictionary does not contain expected key 'key'")
    )

    # Test dictionary value mismatch
    assert evaluator.evaluate(MockContext(output={'key': 'different'})) == snapshot(
        EvaluationReason(
            value=False,
            reason="Output dictionary has different value for key 'key': 'different' != 'value'",
        )
    )

    # Test non-dict value in dict
    evaluator_single = Contains(value='key')
    assert evaluator_single.evaluate(MockContext(output={'key': 'value'})) == snapshot(EvaluationReason(value=True))


async def test_contains_list():
    """Test Contains evaluator with lists."""
    evaluator = Contains(value=42)

    # Test list containment
    assert evaluator.evaluate(MockContext(output=[1, 42, 3])) == snapshot(EvaluationReason(value=True))

    # Test list non-containment
    assert evaluator.evaluate(MockContext(output=[1, 2, 3])) == snapshot(
        EvaluationReason(value=False, reason='Output [1, 2, 3] does not contain provided value')
    )


async def test_contains_as_strings():
    """Test Contains evaluator with as_strings=True."""
    evaluator = Contains(value=42, as_strings=True)

    # Test string conversion
    assert evaluator.evaluate(MockContext(output='The answer is 42')).value is True

    # Test string conversion with non-string types
    assert evaluator.evaluate(MockContext(output=[1, 42, 3])).value is True


async def test_contains_invalid_type():
    """Test Contains evaluator with invalid types."""
    evaluator = Contains(value=42)

    # Test with unhashable type
    class Unhashable:
        __hash__ = None  # type: ignore

    result = evaluator.evaluate(MockContext(output=Unhashable()))
    assert result.value is False
    assert result.reason == "Containment check failed: argument of type 'Unhashable' is not iterable"


async def test_is_instance():
    """Test IsInstance evaluator."""
    evaluator = IsInstance(type_name='str')

    # Test matching type
    assert evaluator.evaluate(MockContext(output='test')).value is True

    # Test non-matching type
    result = evaluator.evaluate(MockContext(output=42))
    assert result.value is False
    assert result.reason == 'output is of type int'

    # Test with class having different qualname
    class OuterClass:
        class InnerClass:
            pass

    evaluator = IsInstance(type_name='InnerClass')
    result = evaluator.evaluate(MockContext(output=OuterClass.InnerClass()))
    assert result.value is True


async def test_max_duration():
    """Test MaxDuration evaluator."""
    # Test with float seconds
    evaluator = MaxDuration(seconds=1.0)
    assert evaluator.evaluate(MockContext(duration=0.5)) is True
    assert evaluator.evaluate(MockContext(duration=1.5)) is False

    # Test with timedelta
    evaluator = MaxDuration(seconds=timedelta(seconds=1))
    assert evaluator.evaluate(MockContext(duration=0.5)) is True
    assert evaluator.evaluate(MockContext(duration=1.5)) is False


@pytest.mark.anyio
async def test_llm_judge_evaluator(mocker: MockerFixture):
    """Test LLMJudge evaluator."""
    # Create a mock GradingOutput
    mock_grading_output = mocker.MagicMock()
    mock_grading_output.pass_ = True
    mock_grading_output.reason = 'Test passed'

    # Mock the judge_output function
    mock_judge_output = mocker.patch('pydantic_evals.evaluators.llm_as_a_judge.judge_output')
    mock_judge_output.return_value = mock_grading_output

    # Mock the judge_input_output function
    mock_judge_input_output = mocker.patch('pydantic_evals.evaluators.llm_as_a_judge.judge_input_output')
    mock_judge_input_output.return_value = mock_grading_output

    ctx = EvaluatorContext(
        name='test',
        inputs={'prompt': 'Hello'},
        metadata=None,
        expected_output=None,
        output='Hello world',
        duration=0.0,
        _span_tree=SpanTreeRecordingError('spans were not recorded'),
        attributes={},
        metrics={},
    )

    # Test without input
    evaluator = LLMJudge(rubric='Content contains a greeting')
    result = await evaluator.evaluate(ctx)
    assert isinstance(result, EvaluationReason)
    assert result.value is True
    assert result.reason == 'Test passed'

    mock_judge_output.assert_called_once_with('Hello world', 'Content contains a greeting', None, None)

    # Test with input
    evaluator = LLMJudge(rubric='Output contains input', include_input=True, model='openai:gpt-4o')
    result = await evaluator.evaluate(ctx)
    assert isinstance(result, EvaluationReason)
    assert result.value is True
    assert result.reason == 'Test passed'

    mock_judge_input_output.assert_called_once_with(
        {'prompt': 'Hello'}, 'Hello world', 'Output contains input', 'openai:gpt-4o', None
    )

    # Test with failing result
    mock_grading_output.pass_ = False
    mock_grading_output.reason = 'Test failed'
    result = await evaluator.evaluate(ctx)
    assert isinstance(result, EvaluationReason)
    assert result.value is False
    assert result.reason == 'Test failed'


@pytest.mark.anyio
async def test_llm_judge_evaluator_with_model_settings(mocker: MockerFixture):
    """Test LLMJudge evaluator with specific model_settings."""
    mock_grading_output = mocker.MagicMock()
    mock_grading_output.pass_ = True
    mock_grading_output.reason = 'Test passed with settings'

    mock_judge_output = mocker.patch('pydantic_evals.evaluators.llm_as_a_judge.judge_output')
    mock_judge_output.return_value = mock_grading_output

    mock_judge_input_output = mocker.patch('pydantic_evals.evaluators.llm_as_a_judge.judge_input_output')
    mock_judge_input_output.return_value = mock_grading_output

    custom_model_settings = ModelSettings(temperature=0.77)

    ctx = EvaluatorContext(
        name='test_custom_settings',
        inputs={'prompt': 'Hello Custom'},
        metadata=None,
        expected_output=None,
        output='Hello world custom settings',
        duration=0.0,
        _span_tree=SpanTreeRecordingError('spans were not recorded'),
        attributes={},
        metrics={},
    )

    # Test without input, with custom model_settings
    evaluator_no_input = LLMJudge(rubric='Greeting with custom settings', model_settings=custom_model_settings)
    result_no_input = await evaluator_no_input.evaluate(ctx)
    assert result_no_input.value is True
    assert result_no_input.reason == 'Test passed with settings'
    mock_judge_output.assert_called_once_with(
        'Hello world custom settings', 'Greeting with custom settings', None, custom_model_settings
    )

    # Test with input, with custom model_settings
    evaluator_with_input = LLMJudge(
        rubric='Output contains input with custom settings',
        include_input=True,
        model='openai:gpt-3.5-turbo',
        model_settings=custom_model_settings,
    )
    result_with_input = await evaluator_with_input.evaluate(ctx)
    assert result_with_input.value is True
    assert result_with_input.reason == 'Test passed with settings'
    mock_judge_input_output.assert_called_once_with(
        {'prompt': 'Hello Custom'},
        'Hello world custom settings',
        'Output contains input with custom settings',
        'openai:gpt-3.5-turbo',
        custom_model_settings,
    )


async def test_python():
    """Test Python evaluator."""
    evaluator = Python(expression='ctx.output > 0')

    # Test with valid expression
    assert evaluator.evaluate(MockContext(output=42)) is True
    assert evaluator.evaluate(MockContext(output=-1)) is False

    # Test with invalid expression
    evaluator_invalid = Python(expression='invalid syntax')
    with pytest.raises(SyntaxError):
        evaluator_invalid.evaluate(MockContext(output=42))


async def test_python_evaluator():
    """Test Python evaluator."""
    ctx = EvaluatorContext(
        name='test',
        inputs={'x': 42},
        metadata=None,
        expected_output=None,
        output={'y': 84},
        duration=0.0,
        _span_tree=SpanTreeRecordingError('did not record spans'),
        attributes={},
        metrics={},
    )

    # Test simple expression
    evaluator = Python(expression='ctx.output["y"] == 84')
    assert evaluator.evaluate(ctx) is True

    # Test accessing inputs
    evaluator = Python(expression='ctx.inputs["x"] * 2 == ctx.output["y"]')
    assert evaluator.evaluate(ctx) is True

    # Test complex expression
    evaluator = Python(expression='all(k in ctx.output for k in ["y"])')
    assert evaluator.evaluate(ctx) is True

    # Test invalid expression
    evaluator = Python(expression='invalid syntax')
    with pytest.raises(SyntaxError):
        evaluator.evaluate(ctx)

    # Test expression with undefined variables
    evaluator = Python(expression='undefined_var')
    with pytest.raises(NameError):
        evaluator.evaluate(ctx)

    # Test expression with type error
    evaluator = Python(expression='ctx.output + 1')  # Can't add dict and int
    with pytest.raises(TypeError):
        evaluator.evaluate(ctx)


def test_default_evaluators():
    """Test DEFAULT_EVALUATORS tuple."""
    # Verify that Python evaluator is not included for security reasons
    assert Python not in DEFAULT_EVALUATORS


async def test_span_query_evaluator(capfire: CaptureLogfire):
    """Test HasMatchingSpan evaluator."""
    # Create a span tree with a known structure
    with context_subtree() as tree:
        with logfire.span('root'):
            with logfire.span('child1', key='value'):
                pass
            with logfire.span('child2'):
                with logfire.span('grandchild', nested=True):
                    pass

    ctx = EvaluatorContext(
        name='test',
        inputs={},
        metadata=None,
        expected_output=None,
        output={},
        duration=0.0,
        _span_tree=tree,
        attributes={},
        metrics={},
    )

    # Test matching by name
    evaluator = HasMatchingSpan(query=SpanQuery(name_equals='child1'))
    assert evaluator.evaluate(ctx) is True

    # Test matching by name pattern
    evaluator = HasMatchingSpan(query=SpanQuery(name_matches_regex='child.*'))
    assert evaluator.evaluate(ctx) is True

    # Test matching by attributes
    evaluator = HasMatchingSpan(query=SpanQuery(has_attributes={'key': 'value'}))
    assert evaluator.evaluate(ctx) is True

    # Test matching nested span
    evaluator = HasMatchingSpan(query=SpanQuery(name_equals='grandchild', has_attributes={'nested': True}))
    assert evaluator.evaluate(ctx) is True

    # Test non-matching query
    evaluator = HasMatchingSpan(query=SpanQuery(name_equals='nonexistent'))
    assert evaluator.evaluate(ctx) is False

    # Test non-matching attributes
    evaluator = HasMatchingSpan(query=SpanQuery(name_equals='child1', has_attributes={'wrong': 'value'}))
    assert evaluator.evaluate(ctx) is False
