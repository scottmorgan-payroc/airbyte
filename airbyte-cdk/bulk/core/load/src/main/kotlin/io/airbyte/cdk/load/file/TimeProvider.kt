/*
 * Copyright (c) 2024 Airbyte, Inc., all rights reserved.
 */

package io.airbyte.cdk.load.file

import io.micronaut.context.annotation.Secondary
import jakarta.inject.Singleton
import org.joda.time.DateTime

interface TimeProvider {
    fun currentTimeMillis(): Long
    suspend fun delay(ms: Long)
    fun now(): DateTime = DateTime(currentTimeMillis())
}

@Singleton
@Secondary
class DefaultTimeProvider : TimeProvider {
    override fun currentTimeMillis(): Long {
        return System.currentTimeMillis()
    }

    override suspend fun delay(ms: Long) {
        kotlinx.coroutines.delay(ms)
    }
}
