package lab.searchops.service;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import lab.searchops.domain.ApiModels.Strategy;
import lab.searchops.domain.StrategyConfig;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class StrategyRepository {
    private static final String SELECT = """
            SELECT id, version, name, status, config::text, created_by, created_at,
                   approved_by, approved_at, published_at, supersedes_version
            FROM search_strategies
            """;

    private final JdbcTemplate jdbc;
    private final ObjectMapper objectMapper;

    public StrategyRepository(JdbcTemplate jdbc, ObjectMapper objectMapper) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
    }

    public Strategy current() {
        return jdbc.query(SELECT + " WHERE status = 'PUBLISHED'", this::map).stream()
                .findFirst().orElseThrow(() -> new IllegalStateException("No published strategy"));
    }

    public List<Strategy> history() {
        return jdbc.query(SELECT + " ORDER BY version DESC", this::map);
    }

    public Optional<Strategy> byId(UUID id) {
        return jdbc.query(SELECT + " WHERE id = ?", this::map, id).stream().findFirst();
    }

    public Optional<Strategy> byVersion(int version) {
        return jdbc.query(SELECT + " WHERE version = ?", this::map, version).stream().findFirst();
    }

    public int nextVersion() {
        return jdbc.queryForObject("SELECT COALESCE(MAX(version), 0) + 1 FROM search_strategies",
                Integer.class);
    }

    public Strategy insertDraft(String name, StrategyConfig config, String actor, Integer supersedes) {
        var id = UUID.randomUUID();
        var version = nextVersion();
        jdbc.update("""
                INSERT INTO search_strategies
                  (id, version, name, status, config, created_by, supersedes_version)
                VALUES (?, ?, ?, 'DRAFT', ?::jsonb, ?, ?)
                """, id, version, name, json(config), actor, supersedes);
        return byId(id).orElseThrow();
    }

    public Strategy transition(UUID id, String status, String actor, String tokenHash) {
        switch (status) {
            case "IN_REVIEW" -> jdbc.update(
                    "UPDATE search_strategies SET status = 'IN_REVIEW', submitted_at = now() WHERE id = ?",
                    id);
            case "APPROVED" -> jdbc.update("""
                    UPDATE search_strategies SET status = 'APPROVED', approved_by = ?,
                      approved_at = now(), approval_token_hash = ? WHERE id = ?
                    """, actor, tokenHash, id);
            case "PUBLISHED" -> jdbc.update("""
                    UPDATE search_strategies SET status = 'PUBLISHED', published_at = now(),
                      approval_token_hash = COALESCE(?, approval_token_hash) WHERE id = ?
                    """, tokenHash, id);
            default -> throw new IllegalArgumentException("Unsupported transition: " + status);
        }
        return byId(id).orElseThrow();
    }

    public void retireCurrent(String status) {
        jdbc.update("UPDATE search_strategies SET status = ? WHERE status = 'PUBLISHED'", status);
    }

    public String tokenHash(UUID id) {
        return jdbc.queryForObject(
                "SELECT approval_token_hash FROM search_strategies WHERE id = ?", String.class, id);
    }

    public void audit(String actor, String requestId, String action, String idempotencyKey,
            Integer before, Integer after, String outcome, Object details) {
        jdbc.update("""
                INSERT INTO audit_logs
                  (actor, request_id, action, idempotency_key, before_version, after_version, outcome, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb)
                """, actor, requestId, action, idempotencyKey, before, after, outcome, json(details));
    }

    public Optional<JsonNode> idempotent(String key, String operation) {
        return jdbc.query(
                "SELECT response::text FROM idempotency_results WHERE idempotency_key = ? AND operation = ?",
                (rs, row) -> readTree(rs.getString(1)), key, operation).stream().findFirst();
    }

    public void saveIdempotent(String key, String operation, Object response) {
        jdbc.update("""
                INSERT INTO idempotency_results (idempotency_key, operation, response)
                VALUES (?, ?, ?::jsonb) ON CONFLICT DO NOTHING
                """, key, operation, json(response));
    }

    public List<java.util.Map<String, Object>> auditLog(int limit) {
        return jdbc.queryForList("""
                SELECT id, actor, request_id, action, idempotency_key, before_version,
                       after_version, outcome, details, created_at
                FROM audit_logs ORDER BY id DESC LIMIT ?
                """, limit);
    }

    private Strategy map(ResultSet rs, int rowNum) throws SQLException {
        try {
            return new Strategy(
                    rs.getObject("id", UUID.class),
                    rs.getInt("version"),
                    rs.getString("name"),
                    rs.getString("status"),
                    objectMapper.readValue(rs.getString("config"), StrategyConfig.class),
                    rs.getString("created_by"),
                    rs.getObject("created_at", java.time.OffsetDateTime.class),
                    rs.getString("approved_by"),
                    rs.getObject("approved_at", java.time.OffsetDateTime.class),
                    rs.getObject("published_at", java.time.OffsetDateTime.class),
                    rs.getObject("supersedes_version", Integer.class));
        } catch (JacksonException exception) {
            throw new SQLException("Invalid strategy JSON", exception);
        }
    }

    private String json(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JacksonException exception) {
            throw new IllegalArgumentException("Unable to serialize JSON", exception);
        }
    }

    private JsonNode readTree(String value) {
        try {
            return objectMapper.readTree(value);
        } catch (JacksonException exception) {
            throw new IllegalArgumentException("Unable to parse stored response", exception);
        }
    }
}
