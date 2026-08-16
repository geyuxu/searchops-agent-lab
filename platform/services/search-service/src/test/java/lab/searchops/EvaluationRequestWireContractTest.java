package lab.searchops;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

import java.util.function.Consumer;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.jackson.autoconfigure.JacksonAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import tools.jackson.databind.json.JsonMapper;

/**
 * POST /api/v1/evaluations/run 请求体的线格式契约。
 *
 * <p>单独成一个类，因为它守的是与 EvaluationAiToggleTest 不同的东西：那边测的是
 * useAi 这个布尔值在 service 里怎么流动，这边测的是**外部调用方省略 use_ai 时会发生什么**。
 *
 * <p>用 ApplicationContextRunner 拿 Spring Boot 真正装配出来的 JsonMapper，而不是手搓一个：
 * Spring Boot 4 / Jackson 3 的默认值与 Jackson 2 不同，手搓 mapper 测不出线上行为，
 * 而 Spring MVC 反序列化 @RequestBody 用的正是这个 bean。
 */
class EvaluationRequestWireContractTest {
    /** data/scripts/evaluate.py:26 逐字节的载荷形状——注意压根没有 use_ai 这个键。 */
    private static final String EVALUATE_PY_PAYLOAD = """
            {"queries":[{"query_id":1,"query":"smart tv","judgments":{"P1":"E"}}],\
            "k":10,"persist":true}""";

    /**
     * 基线红线：请求体省略 use_ai 时必须反序列化成 false，而不是报错。
     *
     * <p>make evaluate 走的就是这条路径。一旦缺省的 use_ai 变成反序列化失败，
     * Spring MVC 会把它翻成 400，evaluate.py 直接抛 HTTPError——
     * 200 条查询的基线（strategy v7, NDCG@10 0.4326）就再也跑不出来了，
     * 而"新增字段向后兼容"这个前提也随之作废。
     *
     * <p>反过来，如果有人把缺省值改成 true，AI 会悄悄混进基线跑，
     * 之后所有 AI 前后对比的数字都不可信。两个方向都由这条测试挡住。
     */
    @Test
    void payloadWithoutUseAiDeserialisesToFalseInsteadOfFailing() {
        withBootJsonMapper(mapper -> {
            assertThatCode(() -> mapper.readValue(EVALUATE_PY_PAYLOAD, EvaluationRequest.class))
                    .as("data/scripts/evaluate.py 不发 use_ai；缺省必须落到 false，"
                            + "否则 POST /api/v1/evaluations/run 返回 400，基线跑不起来")
                    .doesNotThrowAnyException();

            var request = mapper.readValue(EVALUATE_PY_PAYLOAD, EvaluationRequest.class);
            assertThat(request.useAi()).isFalse();
            assertThat(request.k()).isEqualTo(10);
            assertThat(request.persist()).isTrue();
            assertThat(request.queries()).hasSize(1);
        });
    }

    /** 显式传 use_ai 时要被正确读出来，否则新开关等于没接上。 */
    @Test
    void explicitUseAiIsDeserialised() {
        withBootJsonMapper(mapper -> {
            var payload = EVALUATE_PY_PAYLOAD.replace("\"persist\":true",
                    "\"persist\":true,\"use_ai\":true");
            assertThat(mapper.readValue(payload, EvaluationRequest.class).useAi()).isTrue();
        });
    }

    /**
     * 不启动 web 容器、不启动 Testcontainers，只跑 Jackson 的自动装配，
     * 但 spring.jackson.* 的解析路径与线上完全一致。
     */
    private void withBootJsonMapper(Consumer<JsonMapper> assertions) {
        new ApplicationContextRunner()
                .withConfiguration(AutoConfigurations.of(JacksonAutoConfiguration.class))
                // 与 application.yml 保持一致，避免这里测的是另一套 Jackson 配置。
                .withPropertyValues("spring.jackson.default-property-inclusion=non_null")
                .run(context -> assertions.accept(context.getBean(JsonMapper.class)));
    }
}
