package lab.searchops;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Map;
import lab.searchops.domain.ApiModels.ApprovalResponse;
import lab.searchops.domain.ApiModels.CreateStrategyRequest;
import lab.searchops.domain.ApiModels.PublishRequest;
import lab.searchops.domain.ApiModels.RollbackRequest;
import lab.searchops.domain.ApiModels.Strategy;
import lab.searchops.domain.ApiModels.TransitionRequest;
import lab.searchops.domain.StrategyConfig;
import lab.searchops.service.StrategyRepository;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.NONE, properties = {
        "lab.search.elasticsearch-url=http://127.0.0.1:1",
        "lab.search.ai-enabled=false",
        "lab.search.approval-secret=test-secret"
})
@Testcontainers
class StrategyWorkflowIntegrationTest {
    @Container
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17.6-alpine")
            .withDatabaseName("searchops").withUsername("searchops").withPassword("searchops");

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        registry.add("spring.datasource.username", POSTGRES::getUsername);
        registry.add("spring.datasource.password", POSTGRES::getPassword);
    }

    @Autowired StrategyService service;
    @Autowired StrategyRepository repository;

    @Test
    void createApprovePublishRollbackIsIdempotentAndAudited() {
        var baseline = service.current();
        var config = new StrategyConfig(Map.of(), java.util.List.of(), java.util.List.of("product-1"),
                java.util.List.of(), Map.of(), null, 0.0);
        var create = new CreateStrategyRequest("Pinned test", "author", "request-create", config);
        var draft = (Strategy) service.create("create-1", create);
        var replay = service.create("create-1", create);
        assertThat(replay.toString()).contains(draft.id().toString());

        service.submit(draft.id(), "submit-1", new TransitionRequest("author", "request-submit"));
        var approved = (ApprovalResponse) service.approve(draft.id(), "approve-1",
                new TransitionRequest("approver", "request-approve"));
        var published = (Strategy) service.publish(draft.id(), "publish-1",
                new PublishRequest("publisher", "request-publish", approved.approvalToken()));
        assertThat(published.status()).isEqualTo("PUBLISHED");
        assertThat(service.current().version()).isEqualTo(published.version());

        var rolled = (Strategy) service.rollback("rollback-1",
                new RollbackRequest(baseline.version(), "publisher", "request-rollback",
                        approved.approvalToken()));
        assertThat(rolled.status()).isEqualTo("PUBLISHED");
        assertThat(rolled.config()).isEqualTo(baseline.config());
        assertThat(repository.auditLog(20)).extracting(row -> row.get("action"))
                .contains("STRATEGY_CREATE", "STRATEGY_SUBMIT", "STRATEGY_APPROVE",
                        "STRATEGY_PUBLISH", "STRATEGY_ROLLBACK");
    }
}

