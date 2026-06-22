# Scraper CLIs

A collection of headless CLI tools for various data platforms and insights services. Each CLI is designed to be zero-dependency (Python stdlib only) and provides programmatic access to platform functionality through reverse-engineered APIs.

## Structure

```
scraper_clis/
├── numerator/           # Numerator Insights CLI
├── ...                  # Additional platform CLIs
└── README.md           # This file
```

## Available CLIs

### Numerator Insights CLI

Located in the `numerator/` directory. See `numerator/README.md` for complete documentation.

**Features:**
- Zero-dependency Python CLI (stdlib only)
- Full Numerator Insights platform access
- Report execution and data export
- Custom group management
- Narrative analyses
- Session management with Okta authentication

**Quick Start:**
```bash
cd numerator
export NMR_USER='you@example.com'
export NMR_PASS='your-password'
./numerator.py login
./numerator.py docs --long
```

## Security

- All CLIs use environment variables or command-line flags for credentials
- No credentials are ever stored in code or configuration files
- Session data is cached locally in user home directory only
- Capture files containing raw network traffic are gitignored

## Contributing

When adding new CLIs:
1. Create a new directory for the platform
2. Follow the zero-dependency pattern when possible
3. Include comprehensive documentation
4. Ensure proper .gitignore rules for sensitive data
5. Use environment variables for credentials

## License

Internal use only - proprietary tools for PipInsights platform integration.