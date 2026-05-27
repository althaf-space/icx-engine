package com.example.blog.service

import com.example.blog.entity.Post
import com.example.blog.repository.PostRepository
import org.springframework.stereotype.Service

@Service
class PostService(
    private val postRepository: PostRepository,
    private val userService: UserService,
) {
    fun listForAuthor(authorId: Long): List<Post> {
        val author = userService.get(authorId) ?: return emptyList()
        return postRepository.findByAuthor(author)
    }
}
