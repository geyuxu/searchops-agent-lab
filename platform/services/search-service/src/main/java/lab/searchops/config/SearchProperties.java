package lab.searchops.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "lab.search")
public record SearchProperties(
        String elasticsearchUrl,
        String indexAlias,
        String aiAdapterUrl,
        boolean aiEnabled,
        int aiTimeoutMs,
        String approvalSecret,
        String processedDataPath) {}

