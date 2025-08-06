# Triage Agent Workflow Experiments

This directory contains experimental implementations of the triage agent using the BeeAI framework workflows. These implementations demonstrate different approaches to structuring the triage process using workflows.

## Overview

The original triage agent (`triage_agent.py`) uses a single agent with requirements and conditional logic. The workflow-based implementations break down the triage process into discrete steps that can be more easily tested, monitored, and modified.

## Implementations

### 1. Basic Workflow (`triage_agent_workflow.py`)

A simple workflow implementation that demonstrates the basic workflow pattern:

- **State Management**: Uses a `TriageState` model to track progress through the workflow
- **Step-by-step Processing**: Breaks down triage into discrete steps
- **Type Safety**: Uses TypeAlias for workflow steps and proper typing
- **Error Handling**: Includes error handling at the workflow level

**Key Features:**
- Sequential workflow steps
- State persistence throughout the workflow
- Basic error handling
- Redis queue integration

### 2. Multi-Agent Workflow (`triage_agent_multi_workflow.py`)

A multi-agent workflow implementation that uses specialized agents for different aspects of triage:

- **Specialized Agents**: Each agent focuses on a specific aspect of triage
- **Agent Collaboration**: Agents work together to complete the triage process
- **Role-based Processing**: Clear separation of concerns between agents

**Agents:**
- **JiraAnalyzer**: Analyzes Jira issues and extracts key information
- **PackageValidator**: Validates package information and repository structure
- **FixInvestigator**: Investigates potential fixes and validates patches
- **DecisionMaker**: Makes final triage decisions based on all collected information

### 3. Advanced Workflow (`triage_agent_advanced_workflow.py`)

An advanced workflow implementation that demonstrates more sophisticated workflow patterns:

- **Memory Integration**: Uses BeeAI memory for conversation context
- **Observability**: Includes observability setup for monitoring
- **Conditional Logic**: Demonstrates conditional workflow paths
- **Error Recovery**: Includes dedicated error handling steps

**Key Features:**
- Memory persistence throughout the workflow
- Observability and monitoring
- Conditional workflow paths
- Comprehensive error handling
- State validation at each step

## Usage

### Environment Setup

All implementations require the same environment variables as the original triage agent:

```bash
export CHAT_MODEL="your-chat-model"
export MCP_GATEWAY_URL="your-mcp-gateway-url"
export REDIS_URL="your-redis-url"
export COLLECTOR_ENDPOINT="your-collector-endpoint"
export MAX_RETRIES="3"
```

### Direct Mode

Run with a specific Jira issue:

```bash
export JIRA_ISSUE="RHEL-12345"
python triage_agent_workflow.py
python triage_agent_multi_workflow.py
python triage_agent_advanced_workflow.py
```

### Queue Mode

Run in queue mode to process tasks from Redis:

```bash
python triage_agent_workflow.py
python triage_agent_multi_workflow.py
python triage_agent_advanced_workflow.py
```

## Workflow Patterns Demonstrated

### 1. Basic Workflow Pattern

```python
class TriageAgentWorkflow:
    def __init__(self) -> None:
        self.workflow = Workflow[TriageState, WorkflowStep](
            name="TriageAgentWorkflow", 
            schema=TriageState
        )
        
        # Add workflow steps
        self.workflow.add_step("analyze_jira_issue", self._analyze_jira_issue)
        self.workflow.add_step("identify_package", self._identify_package)
        # ... more steps
```

### 2. Multi-Agent Workflow Pattern

```python
class TriageMultiAgentWorkflow:
    def __init__(self) -> None:
        self.workflow = AgentWorkflow(name="TriageMultiAgentWorkflow")
        
        # Add specialized agents
        self.workflow.add_agent(
            name="JiraAnalyzer",
            role="A specialized agent for analyzing Jira issues",
            instructions="...",
            llm=self.llm,
        )
        # ... more agents
```

### 3. Advanced Workflow with Memory

```python
class TriageAdvancedState(BaseModel):
    jira_issue: str
    memory: InstanceOf[UnconstrainedMemory]
    # ... other state fields

class TriageAdvancedWorkflow:
    def __init__(self) -> None:
        self.workflow = Workflow[TriageAdvancedState, WorkflowStep](
            name="TriageAdvancedWorkflow", 
            schema=TriageAdvancedState
        )
```

## Benefits of Workflow Approach

### 1. Modularity
- Each step can be tested independently
- Easy to modify or replace individual steps
- Clear separation of concerns

### 2. Observability
- Each step can be monitored separately
- Progress tracking through the workflow
- Better error isolation and debugging

### 3. Flexibility
- Easy to add new steps or modify existing ones
- Conditional logic can be implemented at the workflow level
- Different workflow paths for different scenarios

### 4. State Management
- Centralized state management
- Type-safe state transitions
- Clear data flow between steps

### 5. Error Handling
- Dedicated error handling steps
- Graceful degradation
- Better error recovery mechanisms

## Comparison with Original Implementation

| Aspect | Original Agent | Workflow Approach |
|--------|----------------|-------------------|
| **Structure** | Single agent with requirements | Discrete workflow steps |
| **Testing** | End-to-end testing | Step-by-step testing |
| **Modification** | Requires understanding entire agent | Modify individual steps |
| **Observability** | Limited step visibility | Full workflow visibility |
| **Error Handling** | Global error handling | Step-specific error handling |
| **State Management** | Implicit state | Explicit state management |

## Next Steps

1. **Integration Testing**: Test the workflow implementations with real Jira issues
2. **Performance Comparison**: Compare performance with the original implementation
3. **Tool Integration**: Integrate existing tools more deeply into workflow steps
4. **Advanced Patterns**: Implement more advanced workflow patterns like loops and conditionals
5. **Production Deployment**: Prepare for production deployment with proper monitoring

## Contributing

When adding new workflow implementations:

1. Follow the established patterns for state management
2. Include proper error handling
3. Add comprehensive logging
4. Include observability setup
5. Document the workflow steps and their purposes
6. Add tests for individual workflow steps

## References

- [BeeAI Framework Workflows Documentation](https://framework.beeai.dev/modules/workflows)
- [Original Triage Agent Implementation](triage_agent.py)
- [BeeAI Framework Multi-Agent Workflows](https://framework.beeai.dev/modules/workflows#multi-agent-workflows) 