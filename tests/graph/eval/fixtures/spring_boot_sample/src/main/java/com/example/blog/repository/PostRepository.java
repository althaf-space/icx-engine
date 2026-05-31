package com.example.blog.repository;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.stereotype.Repository;

import com.example.blog.entity.Post;
import com.example.blog.entity.User;

@Repository
public interface PostRepository extends JpaRepository<Post, Long> {
    List<Post> findByAuthor(User author);
    List<Post> findByPublishedTrue();

    @Query("SELECT p FROM Post p JOIN p.author u WHERE u.email = :email")
    List<Post> findByAuthorEmail(String email);
}
