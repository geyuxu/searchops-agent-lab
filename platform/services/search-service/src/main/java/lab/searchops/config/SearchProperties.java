package lab.searchops.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "lab.search")
public record SearchProperties(
        String elasticsearchUrl,
        String indexAlias,
        String aiAdapterUrl,
        boolean aiEnabled,
        // 读超时：等待 AI 适配器返回响应的时间。真实 LLM 的首字节延迟以秒计。
        int aiTimeoutMs,
        // 连接超时：以前完全没设，适配器不可达时只能靠 JDK 默认行为兜底，
        // 无法保证在读超时预算内失败。与读超时分开配置，故障时才能快速降级。
        int aiConnectTimeoutMs,
        String approvalSecret,
        String processedDataPath) {}

