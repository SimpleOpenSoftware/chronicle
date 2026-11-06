#!/usr/bin/env python3
"""
Plugin Generator Script for Chronicle.

Creates boilerplate plugin structure with templates and examples.

Usage:
    uv run python scripts/create_plugin.py my_awesome_plugin
"""
import argparse
import os
import shutil
import sys
from pathlib import Path


def snake_to_pascal(snake_str: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in snake_str.split("_"))


def create_plugin(plugin_name: str, force: bool = False):
    """
    Create a new plugin with boilerplate structure.

    Args:
        plugin_name: Plugin name in snake_case (e.g., my_awesome_plugin)
        force: Overwrite existing plugin if True
    """
    # Validate plugin name
    if not plugin_name.replace("_", "").isalnum():
        print(f"❌ Error: Plugin name must be alphanumeric with underscores")
        print(f"   Got: {plugin_name}")
        print(f"   Example: my_awesome_plugin")
        sys.exit(1)

    # Convert to class name
    class_name = snake_to_pascal(plugin_name) + "Plugin"

    # Get plugins directory (repo root plugins/)
    script_dir = Path(__file__).parent
    backend_dir = script_dir.parent
    plugins_dir = backend_dir.parent.parent / "plugins"
    plugin_dir = plugins_dir / plugin_name

    # Check if plugin already exists
    if plugin_dir.exists():
        if not force:
            print(f"❌ Error: Plugin '{plugin_name}' already exists at {plugin_dir}")
            print(f"   Use --force to overwrite")
            sys.exit(1)
        else:
            # Remove existing directory when using --force
            print(f"🗑️  Removing existing plugin directory: {plugin_dir}")
            shutil.rmtree(plugin_dir)

    # Create plugin directory
    print(f"📁 Creating plugin directory: {plugin_dir}")
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # Create __init__.py
    init_content = f'''"""
{class_name} for Chronicle.

[Brief description of what your plugin does]
"""

from .plugin import {class_name}

__all__ = ['{class_name}']
'''

    init_file = plugin_dir / "__init__.py"
    print(f"📝 Creating {init_file}")
    init_file.write_text(init_content, encoding="utf-8")

    # Create plugin.py with template
    plugin_content = f'''"""
{class_name} implementation.

This plugin [describe what it does].
"""
import logging
from typing import Any, Dict, List, Optional

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


class {class_name}(BasePlugin):
    """
    [Plugin description]

    Subscribes to: [list events you want to subscribe to]
    - transcript.streaming: Real-time transcript segments
    - conversation.complete: When conversation finishes
    - memory.processed: After memory extraction

    Configuration (config/plugins.yml):
        {plugin_name}:
            enabled: true
            events:
              - conversation.complete  # Change to your event
            condition:
              type: always  # or wake_word, regex, etc.
            # Your custom config here:
            my_setting: ${{MY_ENV_VAR}}
    """

    # Declare which access levels this plugin supports
    # Options: 'transcript', 'conversation', 'memory'
    SUPPORTED_ACCESS_LEVELS: List[str] = ['conversation']

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize plugin with configuration.

        Args:
            config: Plugin configuration from config/plugins.yml
        """
        super().__init__(config)

        # Load your custom configuration
        self.my_setting = config.get('my_setting', 'default_value')

        logger.info(f"{class_name} configuration loaded")

    async def initialize(self):
        """
        Initialize plugin resources.

        Called during application startup.
        Use this to:
        - Connect to external services
        - Initialize clients
        - Validate configuration
        - Set up resources

        Raises:
            Exception: If initialization fails
        """
        if not self.enabled:
            logger.info(f"{class_name} is disabled, skipping initialization")
            return

        logger.info(f"Initializing {class_name}...")

        # TODO: Add your initialization code here
        # Example:
        # self.client = SomeClient(self.my_setting)
        # await self.client.connect()

        logger.info(f"✅ {class_name} initialized successfully")

    async def cleanup(self):
        """
        Clean up plugin resources.

        Called during application shutdown.
        Use this to:
        - Close connections
        - Save state
        - Release resources
        """
        logger.info(f"{class_name} cleanup complete")

    # Implement the methods for events you subscribed to:

    async def on_transcript(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Handle transcript.streaming events.

        Context data contains:
            - transcript: str - The transcript text
            - segment_id: str - Unique segment identifier
            - conversation_id: str - Current conversation ID

        For wake_word conditions, router adds:
            - command: str - Command with wake word stripped
            - original_transcript: str - Full transcript

        Args:
            context: Plugin context with transcript data

        Returns:
            PluginResult with success status and optional message
        """
        # TODO: Implement if you subscribed to transcript.streaming
        pass

    async def on_conversation_complete(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Handle conversation.complete events.

        Context data contains:
            - conversation: dict - Full conversation data
            - transcript: str - Complete transcript
            - duration: float - Conversation duration
            - conversation_id: str - Conversation identifier

        Args:
            context: Plugin context with conversation data

        Returns:
            PluginResult with success status and optional message
        """
        try:
            logger.info(f"Processing conversation complete event for user: {{context.user_id}}")

            # Extract data from context
            conversation = context.data.get('conversation', {{}})
            transcript = context.data.get('transcript', '')
            duration = context.data.get('duration', 0)
            conversation_id = context.data.get('conversation_id', 'unknown')

            # TODO: Add your plugin logic here
            # Example:
            # - Process the transcript
            # - Call external APIs
            # - Store data
            # - Trigger actions

            logger.info(f"Processed conversation {{conversation_id}}")

            return PluginResult(
                success=True,
                message="Processing complete",
                data={{'conversation_id': conversation_id}}
            )

        except Exception as e:
            logger.error(f"Error in {class_name}: {{e}}", exc_info=True)
            return PluginResult(
                success=False,
                message=f"Error: {{str(e)}}"
            )

    async def on_memory_processed(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Handle memory.processed events.

        Context data contains:
            - memories: list - Extracted memories
            - conversation: dict - Source conversation
            - memory_count: int - Number of memories created
            - conversation_id: str - Conversation identifier

        Args:
            context: Plugin context with memory data

        Returns:
            PluginResult with success status and optional message
        """
        # TODO: Implement if you subscribed to memory.processed
        pass

    # Add your custom helper methods here:

    async def _my_helper_method(self, data: Any) -> Any:
        """
        Example helper method.

        Args:
            data: Input data

        Returns:
            Processed data
        """
        # TODO: Implement your helper logic
        pass
'''

    plugin_file = plugin_dir / "plugin.py"
    print(f"📝 Creating {plugin_file}")
    plugin_file.write_text(plugin_content, encoding="utf-8")

    # Create README.md
    readme_content = f"""# {class_name}

[Brief description of what your plugin does]

## Features

- Feature 1
- Feature 2
- Feature 3

## Configuration

### Step 1: Environment Variables

Add to `backends/advanced/.env`:

```bash
# {class_name} Configuration
MY_ENV_VAR=your-value-here
```

### Step 2: Plugin Configuration

Add to `config/plugins.yml`:

```yaml
plugins:
  {plugin_name}:
    enabled: true
    events:
      - conversation.complete  # Change to your event
    condition:
      type: always

    # Your custom configuration
    my_setting: ${{MY_ENV_VAR}}
```

### Step 3: Restart Backend

```bash
cd backends/advanced
docker compose restart
```

## How It Works

1. [Step 1 description]
2. [Step 2 description]
3. [Step 3 description]

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `my_setting` | string | `default` | Description of setting |

## Testing

```bash
# Add testing instructions here
```

## Troubleshooting

### Issue 1

Solution 1

### Issue 2

Solution 2

## Development

### File Structure

```
plugins/{plugin_name}/
├── __init__.py           # Plugin exports
├── plugin.py             # Main plugin logic
└── README.md             # This file
```

## License

GNU Affero General Public License v3.0 only - see project LICENSE file for details.
"""

    readme_file = plugin_dir / "README.md"
    print(f"📝 Creating {readme_file}")
    readme_file.write_text(readme_content, encoding="utf-8")

    # Print success message and next steps
    print(f"\n✅ Plugin '{plugin_name}' created successfully!\n")
    print(f"📁 Location: {plugin_dir}\n")
    print(f"📋 Next steps:")
    print(f"  1. Edit {plugin_file}")
    print(f"     - Implement your plugin logic")
    print(f"     - Choose which events to subscribe to")
    print(f"     - Add your configuration options")
    print(f"")
    print(f"  2. Update config/plugins.yml:")
    print(f"     ```yaml")
    print(f"     plugins:")
    print(f"       {plugin_name}:")
    print(f"         enabled: true")
    print(f"         events:")
    print(f"           - conversation.complete")
    print(f"         condition:")
    print(f"           type: always")
    print(f"     ```")
    print(f"")
    print(f"  3. Add environment variables to .env (if needed)")
    print(f"")
    print(f"  4. Restart backend:")
    print(f"     cd backends/advanced && docker compose restart")
    print(f"")
    print(f"📖 Resources:")
    print(f"  - Plugin development guide: docs/plugin-development-guide.md")
    print(f"  - Example plugin: plugins/email_summarizer/")
    print(f"  - Base plugin class: plugins/base.py")


def main():
    parser = argparse.ArgumentParser(
        description="Create a new Chronicle plugin with boilerplate structure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python scripts/create_plugin.py my_awesome_plugin
  uv run python scripts/create_plugin.py slack_notifier
  uv run python scripts/create_plugin.py todo_extractor --force
        """,
    )
    parser.add_argument(
        "plugin_name", help="Plugin name in snake_case (e.g., my_awesome_plugin)"
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing plugin if it exists",
    )

    args = parser.parse_args()

    try:
        create_plugin(args.plugin_name, force=args.force)
    except KeyboardInterrupt:
        print("\n\n❌ Plugin creation cancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error creating plugin: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
