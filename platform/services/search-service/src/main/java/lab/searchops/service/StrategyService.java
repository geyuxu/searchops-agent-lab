package lab.searchops.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import lab.searchops.config.SearchProperties;
import lab.searchops.domain.ApiModels.ApprovalResponse;
import lab.searchops.domain.ApiModels.CreateStrategyRequest;
import lab.searchops.domain.ApiModels.PublishRequest;
import lab.searchops.domain.ApiModels.RollbackRequest;
import lab.searchops.domain.ApiModels.Strategy;
import lab.searchops.domain.ApiModels.TransitionRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class StrategyService {
    private final StrategyRepository repository;
    private final SearchProperties properties;

    public StrategyService(StrategyRepository repository, SearchProperties properties) {
        this.repository = repository;
        this.properties = properties;
    }

    public Strategy current() {
        return repository.current();
    }

    public List<Strategy> history() {
        return repository.history();
    }

    public Strategy byId(UUID id) {
        return repository.byId(id).orElseThrow(() -> new NotFound("Strategy not found: " + id));
    }

    @Transactional
    public Object create(String key, CreateStrategyRequest request) {
        return idempotent(key, "CREATE", () -> {
            var before = repository.current().version();
            var strategy = repository.insertDraft(request.name(), request.config(), request.actor(), before);
            repository.audit(request.actor(), request.requestId(), "STRATEGY_CREATE", key,
                    before, strategy.version(), "SUCCESS", Map.of("strategy_id", strategy.id()));
            return strategy;
        });
    }

    @Transactional
    public Object submit(UUID id, String key, TransitionRequest request) {
        return idempotent(key, "SUBMIT:" + id, () -> {
            var strategy = byId(id);
            requireStatus(strategy, "DRAFT");
            var result = repository.transition(id, "IN_REVIEW", request.actor(), null);
            repository.audit(request.actor(), request.requestId(), "STRATEGY_SUBMIT", key,
                    strategy.version(), result.version(), "SUCCESS", Map.of("strategy_id", id));
            return result;
        });
    }

    @Transactional
    public Object approve(UUID id, String key, TransitionRequest request) {
        return idempotent(key, "APPROVE:" + id, () -> {
            var strategy = byId(id);
            requireStatus(strategy, "IN_REVIEW");
            var token = approvalToken(strategy);
            var result = repository.transition(id, "APPROVED", request.actor(), sha256(token));
            var response = new ApprovalResponse(result, token);
            repository.audit(request.actor(), request.requestId(), "STRATEGY_APPROVE", key,
                    strategy.version(), result.version(), "SUCCESS", Map.of("strategy_id", id));
            return response;
        });
    }

    @Transactional
    public Object publish(UUID id, String key, PublishRequest request) {
        return idempotent(key, "PUBLISH:" + id, () -> {
            var strategy = byId(id);
            requireStatus(strategy, "APPROVED");
            verifyToken(strategy, request.approvalToken());
            var before = repository.current();
            repository.retireCurrent("RETIRED");
            var result = repository.transition(id, "PUBLISHED", request.actor(), null);
            repository.audit(request.actor(), request.requestId(), "STRATEGY_PUBLISH", key,
                    before.version(), result.version(), "SUCCESS", Map.of("strategy_id", id));
            return result;
        });
    }

    @Transactional
    public Object rollback(String key, RollbackRequest request) {
        return idempotent(key, "ROLLBACK:" + request.targetVersion(), () -> {
            var current = repository.current();
            verifyToken(current, request.approvalToken());
            var target = repository.byVersion(request.targetVersion())
                    .orElseThrow(() -> new NotFound("Target version not found: " + request.targetVersion()));
            if (target.version() == current.version()) {
                throw new Conflict("Target is already published");
            }
            var rollback = repository.insertDraft(
                    "Rollback to v" + target.version(), target.config(), request.actor(), current.version());
            repository.retireCurrent("ROLLED_BACK");
            var published = repository.transition(
                    rollback.id(), "PUBLISHED", request.actor(), sha256(request.approvalToken()));
            repository.audit(request.actor(), request.requestId(), "STRATEGY_ROLLBACK", key,
                    current.version(), published.version(), "SUCCESS",
                    Map.of("source_version", target.version(), "strategy_id", published.id()));
            return published;
        });
    }

    private Object idempotent(String key, String operation, java.util.function.Supplier<Object> work) {
        if (key == null || key.isBlank() || key.length() > 200) {
            throw new IllegalArgumentException("Idempotency-Key header is required (max 200 characters)");
        }
        var existing = repository.idempotent(key, operation);
        if (existing.isPresent()) {
            return existing.get();
        }
        var result = work.get();
        repository.saveIdempotent(key, operation, result);
        return result;
    }

    private void requireStatus(Strategy strategy, String expected) {
        if (!expected.equals(strategy.status())) {
            throw new Conflict("Strategy v" + strategy.version() + " must be " + expected
                    + " but is " + strategy.status());
        }
    }

    private String approvalToken(Strategy strategy) {
        try {
            var mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(properties.approvalSecret().getBytes(StandardCharsets.UTF_8),
                    "HmacSHA256"));
            var value = strategy.id() + ":" + strategy.version();
            return "sop_" + Base64.getUrlEncoder().withoutPadding()
                    .encodeToString(mac.doFinal(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to create approval token", exception);
        }
    }

    private void verifyToken(Strategy strategy, String supplied) {
        var expectedHash = repository.tokenHash(strategy.id());
        if (expectedHash == null || supplied == null || !MessageDigest.isEqual(
                expectedHash.getBytes(StandardCharsets.UTF_8),
                sha256(supplied).getBytes(StandardCharsets.UTF_8))) {
            throw new Forbidden("Approval token is invalid for strategy v" + strategy.version());
        }
    }

    private String sha256(String value) {
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException(exception);
        }
    }

    public static class NotFound extends RuntimeException {
        public NotFound(String message) { super(message); }
    }

    public static class Conflict extends RuntimeException {
        public Conflict(String message) { super(message); }
    }

    public static class Forbidden extends RuntimeException {
        public Forbidden(String message) { super(message); }
    }
}
